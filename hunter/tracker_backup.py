"""Timestamped local copies of the bot's SQLite/Excel data stores.

Three independent "families" are backed up, each pruned to
``cfg.TRACKER_BACKUP_KEEP_FILES`` on its own:

- ``tracker_db_*.db``  — ``tracker.db`` (the real, live data store — SQLite,
  WAL mode). Backed up via :meth:`sqlite3.Connection.backup`, NEVER
  ``shutil.copy2``: copying the main file while a WAL sidecar (``-wal``) has
  uncommitted pages produces a torn, unusable snapshot. The backup API talks
  to SQLite's own C-level backup mechanism, which checkpoints WAL pages as it
  goes and is safe to run against a database that is being written to
  concurrently (the bot process itself, mid-hunt).
- ``app_sqlite_*.db``  — optional: job-hunter-api's ``app.sqlite`` (users,
  auth, profiles), only when ``cfg.APP_SQLITE_PATH`` is set and points at an
  existing file. This process doesn't own that database, so the source is
  opened via a read-only URI (``file:...?mode=ro``) rather than a normal
  read-write connection.
- ``tracker_*.xlsx``   — legacy: ``tracker.xlsx``. Prod only writes this file
  on `/export`, so on a normal deployment it simply won't exist; that is not
  an error, the workbook backup is a best-effort extra, not the durability
  guarantee (see docs/improvement-2026-09/06-OPS_PLAN.md M1).

Every produced ``.db`` copy is verified with ``PRAGMA integrity_check``
immediately after the backup finishes — a silent corrupt backup is worse
than no backup, since it's discovered only during a real restore.
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from hunter import config as cfg

logger = logging.getLogger(__name__)


def run_tracker_backup() -> dict:
    """Snapshot tracker.db (+ optional app.sqlite, + legacy tracker.xlsx).

    Returns a JSON-serializable summary: ``ok``, ``copied``, ``skipped``, ``errors``, ``pruned``.
    """
    result: dict = {
        "ok": True,
        "copied": [],
        "skipped": [],
        "errors": [],
        "pruned": 0,
    }
    backup_dir = Path(cfg.TRACKER_BACKUP_DIR)
    backup_dir.mkdir(parents=True, exist_ok=True)

    ts = f"{datetime.now():%Y%m%d_%H%M%S}_{time.perf_counter_ns()}"
    keep = cfg.TRACKER_BACKUP_KEEP_FILES

    # 1. tracker.db — the real, live data store. WAL-safe backup.
    _backup_sqlite(
        "tracker_db",
        Path(cfg.TRACKER_DB_PATH),
        backup_dir,
        ts,
        result,
        read_only=False,
    )
    result["pruned"] += _prune_old(backup_dir, "tracker_db", keep, "db")

    # 2. app.sqlite — job-hunter-api's data store, optional (own volume/host).
    app_sqlite_path = getattr(cfg, "APP_SQLITE_PATH", "")
    if app_sqlite_path:
        app_sqlite = Path(app_sqlite_path)
        if app_sqlite.is_file():
            _backup_sqlite(
                "app_sqlite",
                app_sqlite,
                backup_dir,
                ts,
                result,
                read_only=True,
            )
        else:
            result["skipped"].append(f"app_sqlite: missing ({app_sqlite})")
        result["pruned"] += _prune_old(backup_dir, "app_sqlite", keep, "db")
    # else: APP_SQLITE_PATH unset — this host doesn't run job-hunter-api, or
    # it isn't reachable from this container; nothing to report.

    # 3. tracker.xlsx — legacy, best-effort extra. Prod only writes it on
    # /export, so a normal deployment simply won't have one; that's fine.
    _copy_xlsx("tracker", Path(cfg.TRACKER_PATH), backup_dir, ts, result)
    result["pruned"] += _prune_old(backup_dir, "tracker", keep, "xlsx")

    if result["errors"]:
        result["ok"] = False
    logger.info(
        "[tracker_backup] copied=%s skipped=%s pruned=%s errors=%s",
        result["copied"],
        result["skipped"],
        result["pruned"],
        result["errors"],
    )
    return result


def _record_copied(dest: Path, result: dict) -> None:
    try:
        rel = dest.relative_to(cfg.PROJECT_DIR)
        result["copied"].append(str(rel).replace("\\", "/"))
    except ValueError:
        result["copied"].append(str(dest))


def _copy_xlsx(label: str, src: Path, backup_dir: Path, ts: str, result: dict) -> None:
    """Plain-file copy for the legacy tracker.xlsx snapshot (not live SQLite)."""
    if not src.is_file():
        result["skipped"].append(f"{label}: missing ({src.name})")
        return
    dest = backup_dir / f"{label}_{ts}.xlsx"
    try:
        shutil.copy2(src, dest)
        _record_copied(dest, result)
    except OSError as e:
        result["ok"] = False
        result["errors"].append(f"{label}: {e}")


def _backup_sqlite(
    label: str,
    src: Path,
    backup_dir: Path,
    ts: str,
    result: dict,
    *,
    read_only: bool,
) -> None:
    """Backup a live SQLite database via ``Connection.backup()`` (WAL-safe).

    Never ``shutil.copy2`` a live WAL database — the main file can be
    byte-copied mid-checkpoint while pages that matter still sit in the
    ``-wal`` sidecar, producing a torn snapshot that LOOKS like a valid
    sqlite file (opens fine) but is missing recent writes or outright
    corrupt. ``Connection.backup()`` drives SQLite's own backup API, which
    is safe against a database being written to concurrently.
    """
    if not src.is_file():
        result["skipped"].append(f"{label}: missing ({src.name})")
        return

    dest = backup_dir / f"{label}_{ts}.db"
    src_conn: sqlite3.Connection | None = None
    dest_conn: sqlite3.Connection | None = None
    try:
        if read_only:
            # This process doesn't own this database (e.g. job-hunter-api's
            # app.sqlite) — open it read-only so a missing file raises
            # instead of silently creating an empty one, and so we never
            # risk taking a write lock on someone else's live db.
            uri = f"file:{src.resolve().as_posix()}?mode=ro"
            src_conn = sqlite3.connect(uri, uri=True)
        else:
            src_conn = sqlite3.connect(str(src))
        dest_conn = sqlite3.connect(str(dest))
        src_conn.backup(dest_conn)
    except sqlite3.Error as e:
        result["ok"] = False
        result["errors"].append(f"{label}: backup failed: {e}")
        logger.error("[tracker_backup] %s backup failed: %s", label, e)
        return
    finally:
        if dest_conn is not None:
            dest_conn.close()
        if src_conn is not None:
            src_conn.close()

    _record_copied(dest, result)
    _verify_integrity(label, dest, result)


def _verify_integrity(label: str, dest: Path, result: dict) -> None:
    """Run PRAGMA integrity_check on a freshly-produced backup copy."""
    try:
        conn = sqlite3.connect(str(dest))
        try:
            row = conn.execute("PRAGMA integrity_check").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as e:
        result["ok"] = False
        result["errors"].append(f"{label}: integrity_check error: {e}")
        logger.error("[tracker_backup] %s integrity_check error: %s", label, e)
        return

    status = row[0] if row else "unknown"
    if status != "ok":
        result["ok"] = False
        result["errors"].append(f"{label}: integrity_check failed: {status}")
        logger.error("[tracker_backup] %s integrity_check FAILED: %s", label, status)
    else:
        logger.info("[tracker_backup] %s integrity_check ok", label)


def _prune_old(backup_dir: Path, prefix: str, keep: int, ext: str = "xlsx") -> int:
    if keep <= 0:
        return 0
    files = sorted(
        (p for p in backup_dir.glob(f"{prefix}_*.{ext}") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    removed = 0
    for p in files[keep:]:
        try:
            p.unlink()
            removed += 1
        except OSError as exc:
            logger.warning("[tracker_backup] prune failed %s: %s", p, exc)
    return removed
