"""hunter/erasure.py — erase one user's data in a single operation.

docs/improvement-2026-09/07-COMPLIANCE_PLAN.md risk #1 ("Right to erasure"),
milestone M1, bot side only. The API side (admin.deleteUser calling into this
via a profile_jobs kind='erase' row, plus its own tables like
email_verification_tokens) is a separate change in job-hunter-api.

``erase_user(user_id)`` is the durable-storage half of "delete my data": in
ONE sqlite transaction it deletes every row scoped to ``user_id`` from every
table that HAS a user_id column — discovered via ``PRAGMA table_info`` rather
than a hardcoded list, so a table added later is covered automatically
without a second edit here (``tests/test_erasure.py`` still pins the four
tables known today by name: applications / telegram_links / user_settings /
profile_jobs — plus telegram_link_codes, which also carries a user_id column
and is erased for the same reason). It then removes the ``users/{uid}/``
filesystem tree (candidate.yaml, Applications/, templates/, uploads/,
preview/...) and does a best-effort, NAME-only sweep of ``logs/`` for files
whose filename contains the uid.

Two callers:
  - hunter/schedules/profile_jobs.py's ``kind='erase'`` (the API is expected
    to enqueue this the same way it enqueues render/parse/preview jobs — see
    docs/ERASURE_CONTRACT.md for the payload/result shape)
  - tools/erase_user.py (an owner-run CLI seam, e.g. for a support request
    handled without going through the API)

Refuses to run on:
  - an empty or unsafe ``user_id`` — it becomes a `users/{uid}/` filesystem
    path and a SQL parameter, so it must be a single safe path segment (no
    separators, no `..`, not absolute). Same caution as
    hunter.schedules.profile_jobs._resolve_user_relative_path, scaled down to
    a single segment — there is no join here, so a bare `..` is the whole
    threat surface.
  - DEFAULT_USER_ID (the owner) unless ``force_owner=True`` — an automated
    erasure job is never meant to target the owner's own account, and a
    mistaken uid match here would be catastrophic.

Log cleanup is best-effort and NAME-based only. Today's log filenames
(``logs/apply_stdout/*``, ``logs/dual_shadow/*``, ``logs/apply_failures.jsonl``,
``logs/hunter_errors.log``) do NOT carry a uid — that is M3 in the compliance
plan (see risk #10), still open. Matching on filename here is deliberately
already-correct code waiting for that data to exist: once M3 lands the uid
prefix, erasure picks those files up automatically, with no further change
in this module. Grepping *content* for personal data (a company/URL appears
inside a stdout transcript regardless of filename) is explicitly out of
scope — flagged, not solved, by this milestone.

Also invalidates the in-memory ``hunter.tracker_cache.cache`` singleton when
it could hold rows for the erased user. ``read_all_tracker_rows()`` (which
feeds the cache) filters ``WHERE user_id = current_user_id()`` — the cache in
any one process only ever holds rows for THAT process's own scoped user — so
erasing a user ID that doesn't match `current_user_id()` is guaranteed to be
a no-op for the cache. When it does match (the ``force_owner`` path, or a
future per-user bot process), the cache is cleared and marked unloaded rather
than reloaded synchronously: every existing reader already does
``if not cache.loaded: await cache.load_from_db()`` before using it
(hunter/commands/unsent.py, hunter/schedules/gsheets.py,
hunter/schedules/pending_report.py), so marking it unloaded is enough to make
the next read transparently pick up the post-erasure state — and it works
from a fully synchronous caller (tools/erase_user.py has no event loop at
all) without needing asyncio.run()/a lock, unlike every other cache mutation
in this module.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db

log = logging.getLogger(__name__)

# Module-level so tests can monkeypatch onto an isolated DB (mirrors
# hunter.tracker.DB_PATH / hunter.profile_jobs.DB_PATH).
DB_PATH: Path = TRACKER_DB_PATH

# Test hook for the log-cleanup sweep — mirrors hunter.apply_failures_log's
# own _log_path_override pattern. None (default) resolves to
# hunter.config.PROJECT_DIR / "logs" at call time.
_LOGS_DIR_OVERRIDE: Path | None = None


@dataclass
class ErasureReport:
    """What erase_user() did (or, for a dry run, would have done)."""

    user_id: str
    dry_run: bool
    tables: dict[str, int] = field(default_factory=dict)  # table -> rows removed
    files_removed: int = 0
    bytes_removed: int = 0
    users_dir_removed: bool = False
    log_files_removed: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """docs/ERASURE_CONTRACT.md result shape, plus diagnostic extras."""
        return {
            "user_id": self.user_id,
            "dry_run": self.dry_run,
            "tables": dict(self.tables),
            "files": self.files_removed,
            "bytes": self.bytes_removed,
            "users_dir_removed": self.users_dir_removed,
            "log_files_removed": list(self.log_files_removed),
        }


def _validate_user_id(user_id: str) -> str:
    uid = (user_id or "").strip()
    if not uid:
        raise ValueError("user_id must not be empty")
    if uid in (".", ".."):
        raise ValueError(f"unsafe user_id: {user_id!r}")
    if "/" in uid or "\\" in uid:
        raise ValueError(f"unsafe user_id: {user_id!r}")
    p = Path(uid)
    if p.is_absolute() or len(p.parts) != 1:
        raise ValueError(f"unsafe user_id: {user_id!r}")
    return uid


def _discover_user_id_tables(conn) -> list[str]:
    """Every table in this DB that has a user_id column, sorted for a
    deterministic report. Table names come from sqlite_master itself (never
    from external input), so the f-string PRAGMA below is not an injection
    risk despite the shape."""
    names = [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    out = []
    for name in names:
        cols = {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}  # noqa: S608
        if "user_id" in cols:
            out.append(name)
    return sorted(out)


def _tree_stats(root: Path) -> tuple[int, int]:
    """(file_count, total_bytes) for every regular file under root."""
    count = 0
    total = 0
    for p in root.rglob("*"):
        if p.is_file():
            count += 1
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return count, total


def _logs_dir() -> Path:
    if _LOGS_DIR_OVERRIDE is not None:
        return _LOGS_DIR_OVERRIDE
    from hunter.config import PROJECT_DIR

    return PROJECT_DIR / "logs"


def _cleanup_logs(uid: str) -> list[str]:
    """Best-effort, name-only sweep — see the module docstring for why this
    matches nothing today and is not a bug."""
    logs_dir = _logs_dir()
    removed: list[str] = []
    if not logs_dir.is_dir():
        return removed
    for p in logs_dir.rglob("*"):
        if p.is_file() and uid in p.name:
            try:
                p.unlink()
                removed.append(str(p))
            except OSError as e:
                log.warning("erase_user: failed to remove log file %s: %s", p, e)
    return removed


def _invalidate_tracker_cache(uid: str) -> None:
    try:
        from hunter.config import current_user_id

        if uid != current_user_id():
            return
        from hunter.tracker_cache import cache

        cache.rows.clear()
        cache.by_url.clear()
        cache.by_ctkey.clear()
        cache._loaded = False  # noqa: SLF001 — same-package invalidation, no public setter exists
    except Exception as e:  # noqa: BLE001 — cache invalidation must never break erasure
        log.warning("erase_user: tracker_cache invalidation failed: %s", e)


def erase_user(
    user_id: str,
    *,
    dry_run: bool = False,
    force_owner: bool = False,
    exclude_job_id: str | None = None,
) -> ErasureReport:
    """Erase every row + file scoped to user_id.

    exclude_job_id: when erase_user() is itself invoked from inside the
    profile_jobs drain loop (kind='erase'), the job's own row in
    `profile_jobs` belongs to the very user being erased. Deleting it here
    would pull the row out from under _process_job's normal
    finish_profile_job() call right after this returns, so that terminal
    write is left in place on purpose: pass the running job's own id here to
    exclude it from the profile_jobs bulk-delete, and let the caller stamp
    status='done' on it as usual afterwards. Documented + tested in
    tests/test_erasure.py::TestExcludeJobId.
    """
    uid = _validate_user_id(user_id)

    from hunter.config import DEFAULT_USER_ID

    owner_id = (DEFAULT_USER_ID or os.getenv("DEFAULT_USER_ID", "")).strip()
    if owner_id and uid == owner_id and not force_owner:
        raise ValueError(f"refusing to erase the owner account ({uid!r}) without force_owner=True")

    tables: dict[str, int] = {}
    with get_db(DB_PATH) as conn:
        params: tuple[str, ...]
        for table in _discover_user_id_tables(conn):
            if table == "profile_jobs" and exclude_job_id:
                sql_select = f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=? AND id != ?"  # noqa: S608
                sql_delete = f"DELETE FROM {table} WHERE user_id=? AND id != ?"  # noqa: S608
                params = (uid, exclude_job_id)
            else:
                sql_select = f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?"  # noqa: S608
                sql_delete = f"DELETE FROM {table} WHERE user_id=?"  # noqa: S608
                params = (uid,)
            if dry_run:
                row = conn.execute(sql_select, params).fetchone()
                tables[table] = row["n"]
            else:
                cur = conn.execute(sql_delete, params)
                tables[table] = cur.rowcount

    files_removed = 0
    bytes_removed = 0
    users_dir_removed = False
    from hunter.users import user_paths

    root = user_paths(uid).root
    if root.exists():
        files_removed, bytes_removed = _tree_stats(root)
        if not dry_run:
            shutil.rmtree(root, ignore_errors=False)
            users_dir_removed = True

    log_files_removed: list[str] = []
    if not dry_run:
        log_files_removed = _cleanup_logs(uid)
        _invalidate_tracker_cache(uid)

    return ErasureReport(
        user_id=uid,
        dry_run=dry_run,
        tables=tables,
        files_removed=files_removed,
        bytes_removed=bytes_removed,
        users_dir_removed=users_dir_removed,
        log_files_removed=log_files_removed,
    )
