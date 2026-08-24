"""
hunter/gdrive_sync.py — High-level Google Drive upload logic.

Uploads application folders to Drive after a successful apply.
Best-effort: errors are logged as warnings, never propagated to caller.

Public API:
  upload_application_folder(folder_path) -> str | None
    Upload Applications/{date}/{company}/ to Drive.
    Returns folder URL or None if disabled / error.

  upload_missing_folders(project_dir) -> dict
    Upload all tracker.xlsx folders that exist locally but weren't uploaded yet.
    Returns {"uploaded": int, "skipped_missing": int, "errors": list[str]}
"""

import asyncio
import logging
import re
import tempfile
from datetime import date as _date
from pathlib import Path
from typing import Any

from hunter.best_effort import best_effort
from hunter.config import (
    GDRIVE_ENABLED,
    GDRIVE_ROOT_FOLDER_ID,
    GDRIVE_ROOT_FOLDER_NAME,
    GSHEETS_CREDENTIALS_FILE,
    GSHEETS_TOKEN_FILE,
)

log = logging.getLogger(__name__)

# Matches the start of a log entry: "2026-05-27 21:40:05 [LEVEL] ..."
# Lines that don't match are continuation lines (tracebacks, indented text).
_LOG_HEADER_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

# ---------------------------------------------------------------------------
# Lazy service singleton
# ---------------------------------------------------------------------------

_service: Any = None


def _get_service() -> Any | None:
    """Build and cache the Drive API service. Returns None if disabled or on error."""
    if not GDRIVE_ENABLED:
        return None
    global _service
    if _service is None:
        try:
            from hunter.gdrive_client import build_service

            _service = build_service(GSHEETS_CREDENTIALS_FILE, GSHEETS_TOKEN_FILE)
        except Exception as e:
            log.error("gdrive_sync: failed to build service: %s", e)
    return _service


def _ready() -> bool:
    return bool(GDRIVE_ENABLED and _get_service() is not None)


def _invalidate_service() -> None:
    """Drop the cached Drive service so the next call rebuilds it from disk."""
    global _service
    _service = None


# ---------------------------------------------------------------------------
# Drive API call serialization (M2, docs/GDRIVE_SSL_RACE_PLAN.md)
# ---------------------------------------------------------------------------
# One cached googleapiclient service (`_get_service()` above) sits on one
# httplib2.Http object with one keep-alive TLS socket, and httplib2 is not
# thread-safe (documented upstream). Every upload runs that service inside a
# worker thread (asyncio.to_thread) — two in flight at once write into the
# same TLS stream and read each other's bytes, surfacing as
# `[SSL] record layer failure` / `LENGTH_MISMATCH`. `_DRIVE_LOCK` (renamed
# from `_FOLDER_LOCK`, which only ever covered folder resolution — the file
# uploads it didn't cover are exactly where the SSL race was happening)
# serializes EVERY Drive API call in this process through the one
# `_drive_call()` helper below. Acquired per call, not per pass, so a
# post-apply targeted upload waits at most one call (~seconds) behind a
# backfill pass, not the whole pass. Cross-*process* concurrency (detached
# dual-apply shadows) is unaffected — each process has its own service and
# socket; `gdrive_client._resolve_create_race` remains the guard there.

_DRIVE_LOCK: asyncio.Lock | None = None


def _drive_lock() -> asyncio.Lock:
    # Created lazily: at import time there is no running loop to bind to.
    global _DRIVE_LOCK
    if _DRIVE_LOCK is None:
        _DRIVE_LOCK = asyncio.Lock()
    return _DRIVE_LOCK


async def _drive_call(fn, *args, timeout: float | None = None):
    """Run a blocking Drive API call in a worker thread, serialized against
    every other Drive call in this process, with an optional wall-clock cap.

    `asyncio.to_thread` is not cancellable: on a `timeout`, the awaiting
    coroutine gives up but the worker thread keeps running, still holding the
    (now abandoned) service's socket. Callers that wrap a `_drive_call` in
    their own retry logic on `asyncio.TimeoutError` MUST call
    `_invalidate_service()` too, so the NEXT call builds a fresh service (and
    a fresh, distinct socket) instead of racing the abandoned thread's
    lingering one — see `upload_missing_folders`.
    """
    async with _drive_lock():
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)


async def _resolve_folder(svc: Any, name: str, parent_id: str | None) -> str:
    """get_or_create_folder, serialized against this process's other Drive callers.

    Several coroutines resolve the same date folder concurrently — the
    post-apply delivery hook and the periodic upload-missing backfill overlap
    routinely, and both run in this one event loop. Interleaved, their
    list-then-create each create their own copy of e.g. ``2026-07-06``, which
    Drive happily accepts. Serializing means the second caller's list runs after
    the first's create and therefore FINDS it.

    Deliberately not memoized: an id cached for the process lifetime goes stale
    the moment a folder is moved or trashed by hand, and would then silently
    absorb uploads into the trash. Re-listing costs one cheap API call.
    gdrive_client still guards the cross-*process* race (detached shadow runs),
    which no in-process lock can see.
    """
    from hunter.gdrive_client import get_or_create_folder

    return await _drive_call(get_or_create_folder, svc, name, parent_id)


async def _call_with_reauth(op):
    """Await ``op()``; on an OAuth/refresh error, rebuild the Drive service from
    the current token file and retry ONCE.

    The bot is a single long-lived process that builds one Drive service and
    caches it for its whole lifetime. When Google rotates/expires the refresh
    token mid-run — e.g. a detached dual-apply *shadow* process refreshed and
    persisted a new token to ``gsheets_token.json`` — the bot's in-memory
    credentials go stale and every upload silently fails, while fresh
    short-lived processes (the shadow) and the separately-cached Sheets service
    keep working. Rebuilding from disk picks up the freshest persisted token and
    lets the bot recover WITHOUT a restart (before this, the stale service also
    poisoned the 30-min backfill, so a missed folder stayed missing forever —
    e.g. the Nexters primary CV that never reached Drive on 2026-07-13).

    If the on-disk token is itself revoked, the retry re-raises and the existing
    ``oauth_alert`` boundary in ``build_service`` fires the re-auth alert.
    """
    from hunter.oauth_alert import is_oauth_error

    try:
        return await op()
    except Exception as e:  # noqa: BLE001 — classify, rebuild, retry once
        if not is_oauth_error(e):
            raise
        log.warning("gdrive_sync: OAuth error (%s) — rebuilding service, retrying once", e)
        _invalidate_service()
        return await op()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _do_upload(folder_path: Path) -> str:
    """Core upload logic — raises on error. Called by both public functions."""
    from hunter.gdrive_client import upload_folder, folder_url

    svc = _get_service()
    date_name = folder_path.parent.name

    if GDRIVE_ROOT_FOLDER_ID:
        root_id = GDRIVE_ROOT_FOLDER_ID
    else:
        root_id = await _resolve_folder(svc, GDRIVE_ROOT_FOLDER_NAME, None)

    date_id = await _resolve_folder(svc, date_name, root_id)
    company_id = await _drive_call(upload_folder, svc, folder_path, date_id)
    return folder_url(company_id)


async def upload_application_folder(
    folder_path: Path,
    job_url: str | None = None,
) -> str | None:
    """
    Upload Applications/{date}/{company}/ to Google Drive.

    Drive structure created:
      Job Hunter (or GDRIVE_ROOT_FOLDER_NAME) /
        {date} /
          {company} /
            <all files>

    If job_url is provided, writes the Drive URL back to tracker.xlsx (col 12)
    after a successful upload so the row is not re-uploaded by upload_missing_folders.

    Returns the Drive URL for the company folder, or None on error / disabled.
    """
    if not _ready():
        return None

    if not folder_path.exists() or not folder_path.is_dir():
        log.warning("gdrive_sync: folder not found: %s", folder_path)
        return None

    url: str | None = None
    with best_effort("gdrive.upload_application_folder"):
        try:
            url = await _call_with_reauth(lambda: _do_upload(folder_path))
            log.info("gdrive_sync: uploaded %s → %s", folder_path.name, url)
            if job_url:
                from hunter.tracker import set_drive_url

                await asyncio.to_thread(set_drive_url, job_url, url)
        except Exception as e:
            log.warning("gdrive_sync: upload failed for %s: %s", folder_path, e)
            url = None
            raise
    return url


async def upload_shadow_folder(primary_folder: Path, shadow_subfolder: Path) -> str | None:
    """
    Upload a dual-apply shadow comparison subfolder, nested under the primary's
    company folder on Drive:

      Job Hunter / {date} / {company} / {shadow_name} / <files>

    Unlike upload_application_folder this never writes back to tracker.xlsx —
    the shadow run has no tracker row. Best-effort; returns the shadow folder's
    Drive URL, or None if disabled / missing / error.
    """
    if not _ready():
        return None
    if not shadow_subfolder.exists() or not shadow_subfolder.is_dir():
        return None

    from hunter.gdrive_client import upload_folder, folder_url

    async def _do() -> str:
        svc = _get_service()
        date_name = primary_folder.parent.name

        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await _resolve_folder(svc, GDRIVE_ROOT_FOLDER_NAME, None)
        date_id = await _resolve_folder(svc, date_name, root_id)
        company_id = await _resolve_folder(svc, primary_folder.name, date_id)
        shadow_id = await _drive_call(upload_folder, svc, shadow_subfolder, company_id)
        return folder_url(shadow_id)

    url: str | None = None
    with best_effort("gdrive.upload_shadow_folder"):
        try:
            url = await _call_with_reauth(_do)
            log.info("gdrive_sync: uploaded shadow %s → %s", shadow_subfolder, url)
        except Exception as e:
            log.warning("gdrive_sync: shadow upload failed for %s: %s", shadow_subfolder, e)
            url = None
            raise
    return url


async def delete_application_folder(drive_url: str) -> bool:
    """Delete a Drive folder by its URL (e.g. the one stored in tracker col 12).

    Returns True if deleted, False if disabled / not found / error (best-effort).
    """
    if not _ready():
        return False

    from hunter.gdrive_client import folder_id_from_url, delete_folder

    folder_id = folder_id_from_url(drive_url)
    if not folder_id:
        log.warning(
            "gdrive_sync.delete_application_folder: cannot parse folder_id from %r", drive_url
        )
        return False

    try:
        result = await asyncio.to_thread(delete_folder, _get_service(), folder_id)
        return result
    except Exception as e:
        log.warning("gdrive_sync.delete_application_folder: error deleting %s: %s", folder_id, e)
        return False


async def upload_log_file(
    log_path: Path,
    *,
    date_str: str | None = None,
) -> str | None:
    """Upload today's log entries to Drive as ``Logs/YYYY-MM-DD.log``.

    Filters the log file to lines belonging to *today* so each Drive file
    covers exactly one calendar day.  Same-day calls overwrite the same
    Drive file — it accumulates throughout the day.

    Multi-line entries (tracebacks) are preserved: a line without a timestamp
    header is treated as a continuation of the previous entry and included
    whenever that entry belonged to today.

    Drive structure::

        Job Hunter/
          Logs/
            2026-05-27.log   ← overwritten on each upload, grows through the day
            2026-05-28.log   ← created automatically the next day
            …

    Args:
        log_path: Path to the local log file (``logs/hunter_errors.log``).
        date_str: ISO date to filter by, e.g. ``"2026-05-27"``.
                  Defaults to today.  Pass explicitly in tests.

    Returns:
        Drive file URL or ``None`` if disabled / nothing to upload / error.
    """
    if not _ready():
        return None
    if not log_path.exists() or not log_path.is_file():
        log.debug("gdrive_sync.upload_log_file: %s not found, skipping", log_path)
        return None

    today = date_str or _date.today().isoformat()

    # ── Extract today's lines (keep traceback continuations) ─────────────────
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.warning("gdrive_sync.upload_log_file: cannot read %s: %s", log_path, e)
        return None

    today_lines: list[str] = []
    in_today = False
    for line in content.splitlines(keepends=True):
        if _LOG_HEADER_RE.match(line):  # new log entry
            in_today = line.startswith(today)
        if in_today:
            today_lines.append(line)

    if not today_lines:
        log.debug(
            "gdrive_sync.upload_log_file: no entries for %s in %s — skipping",
            today,
            log_path.name,
        )
        return None

    # ── Write filtered content to a temp file named YYYY-MM-DD.log ───────────
    tmp_dir = Path(tempfile.mkdtemp(prefix="hunter_log_"))
    dated_file = tmp_dir / f"{today}.log"
    try:
        dated_file.write_text("".join(today_lines), encoding="utf-8")

        from hunter.gdrive_client import upload_file

        svc = _get_service()

        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await _resolve_folder(svc, GDRIVE_ROOT_FOLDER_NAME, None)

        logs_folder_id = await _resolve_folder(svc, "Logs", root_id)
        file_id = await _drive_call(upload_file, svc, dated_file, logs_folder_id)
        url = f"https://drive.google.com/file/d/{file_id}/view"
        log.info(
            "gdrive_sync: uploaded %s (%d lines) → %s",
            dated_file.name,
            len(today_lines),
            url,
        )
        return url
    except Exception as e:
        log.warning("gdrive_sync.upload_log_file: failed for %s: %s", today, e)
        return None
    finally:
        # Always clean up temp file + dir
        try:
            dated_file.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tmp_dir.rmdir()
        except Exception:
            pass


_UPLOAD_TIMEOUT = 120  # seconds per folder

# Re-entrancy guard: the delivery fallback (hunter/delivery.py, whenever the
# targeted post-apply upload misses its tracker row) and the periodic
# scheduled_gdrive_upload_missing backfill routinely fire within the same
# second, and a second full pass over the SAME folder list is not just
# unsafe (see the M2 _DRIVE_LOCK below) — it is pointless: the first pass is
# already uploading exactly what the second would. A plain module-level bool
# is enough here (not asyncio.Lock): both callers run in this one process's
# single-threaded event loop, so the check-then-set below never interleaves
# with another coroutine — there is no `await` between them.
_backfill_running = False


# Statuses that mean "no application was shipped", so their folder (when they
# have one at all) must never be uploaded. Kept local to the uploader: tracker
# owns the values, this module owns the delivery policy.
_NON_APPLIED_STATUSES = frozenset({"SKIP", "FAIL", "EXPIRED", "MANUAL"})


async def upload_missing_folders(
    project_dir: Path,
    progress_cb=None,
    *,
    force: bool = False,
) -> dict:
    """Upload tracker.xlsx application folders that haven't been uploaded to Drive yet.

    Skips rows that already have a Drive URL in col 12. After each successful
    upload, writes the Drive URL back to tracker so the row is not re-uploaded.
    Shadow (dual-apply comparison) subfolders are separately skipped via the
    content-signature ledger (hunter.drive_ledger) when unchanged since their
    last successful upload — pass `force=True` to bypass the ledger and
    re-upload every shadow subfolder regardless (the one case the ledger
    gets wrong: a folder deleted on Drive by hand, which the bot can't see).

    progress_cb: optional async callable(str) for Telegram progress updates.

    If a pass is already running (delivery's fallback and the scheduled
    backfill overlapped), this call returns immediately with
    ``"skipped_busy": True`` and zero counters — deliberately not an
    exception, so `best_effort` never counts a busy-skip as a failure.

    Returns:
      {"uploaded": int, "already_uploaded": int, "skipped_missing": int, "errors": list[str],
       "shadow_uploaded": int, "shadow_skipped": int, "shadow_errors": list[str]}
    """
    if not _ready():
        return {
            "uploaded": 0,
            "already_uploaded": 0,
            "skipped_missing": 0,
            "errors": ["GDRIVE_ENABLED is false or service not ready"],
            "shadow_uploaded": 0,
            "shadow_skipped": 0,
            "shadow_errors": [],
        }

    global _backfill_running
    if _backfill_running:
        log.info("gdrive_sync: upload_missing_folders already running — skipping this pass")
        return {
            "uploaded": 0,
            "already_uploaded": 0,
            "skipped_missing": 0,
            "errors": [],
            "shadow_uploaded": 0,
            "shadow_skipped": 0,
            "shadow_errors": [],
            "skipped_busy": True,
        }
    _backfill_running = True
    try:
        return await _upload_missing_folders_locked(project_dir, progress_cb, force=force)
    finally:
        _backfill_running = False


async def _upload_missing_folders_locked(
    project_dir: Path,
    progress_cb=None,
    *,
    force: bool = False,
) -> dict:
    """Body of upload_missing_folders, run under the `_backfill_running` guard."""
    from hunter.gdrive_client import upload_folder, folder_url
    from hunter.tracker import read_all_tracker_rows, set_drive_url

    rows = await asyncio.to_thread(read_all_tracker_rows)

    # Collect folders that need uploading, and (separately) every folder that
    # exists locally — the latter feeds the shadow-subfolder scan below, which
    # runs independently of the per-row "already uploaded" check (dual-apply
    # shadow sets have no tracker row / Drive URL column of their own).
    to_upload: list[tuple[str, str, Path]] = []  # (company, job_url, folder_path)
    existing_folders: set[Path] = set()
    already_uploaded = 0
    skipped_missing = 0

    for row in rows:
        folder_str = row.get("Folder", "").strip()
        if not folder_str:
            continue
        # A folder on a non-applied row is not an application. Until 2026-08-24
        # every SKIP producer wrote folder='' (add_skipped / add_react_skipped
        # have no folder column at all), so "has a folder" was a safe proxy for
        # "was generated" and this check was unnecessary.
        # tracker.convert_own_applied_row is the first producer of a SKIP row
        # that KEEPS its folder -- the post-generation aborts delete the
        # rendered documents but leave job_posting.txt for diagnostics. Without
        # this guard the next backfill pass would upload that near-empty folder
        # and stamp a Drive URL on it, ~30 minutes after the pipeline decided to
        # throw the package away (docs/STACK_PRESCREEN_PLAN.md M1).
        if (row.get("ATS %") or "").strip().upper() in _NON_APPLIED_STATUSES:
            continue
        folder_path = Path(folder_str)
        if not folder_path.is_absolute():
            folder_path = project_dir / folder_str
        if not folder_path.exists() or not folder_path.is_dir():
            skipped_missing += 1
            log.debug("gdrive_sync: folder not found locally, skipping: %s", folder_path)
            continue
        existing_folders.add(folder_path)
        # Skip rows that already have a Drive URL — folder itself doesn't need
        # re-upload, but it's still scanned for shadow subfolders below.
        existing_drive_url = row.get("Drive URL", "").strip()
        if existing_drive_url and existing_drive_url not in ("-", "—"):
            already_uploaded += 1
            continue
        to_upload.append((row.get("Company", folder_path.name), row.get("URL", ""), folder_path))

    shadow_uploaded, shadow_skipped, shadow_errors = await _upload_shadow_subfolders(
        existing_folders, force=force
    )

    if not to_upload:
        return {
            "uploaded": 0,
            "already_uploaded": already_uploaded,
            "skipped_missing": skipped_missing,
            "errors": [],
            "shadow_uploaded": shadow_uploaded,
            "shadow_skipped": shadow_skipped,
            "shadow_errors": shadow_errors,
        }

    errors: list[str] = []
    uploaded = 0

    # Resolve root folder once — avoids a redundant API call per row. Re-fetch
    # the service inside the op so a rebuild on OAuth failure takes effect.
    async def _resolve_root() -> str:
        if GDRIVE_ROOT_FOLDER_ID:
            return GDRIVE_ROOT_FOLDER_ID
        return await asyncio.wait_for(
            _resolve_folder(_get_service(), GDRIVE_ROOT_FOLDER_NAME, None),
            timeout=30,
        )

    root_id = None
    root_error: str | None = None
    with best_effort("gdrive.upload_missing_folders"):
        try:
            root_id = await _call_with_reauth(_resolve_root)
        except asyncio.TimeoutError as e:
            # The wait_for above abandoned a worker thread mid-request — the
            # connection state on the cached service is now unknown. Rebuild
            # from disk so the next call gets a fresh service and socket
            # instead of racing the abandoned thread's lingering one.
            root_error = str(e)
            _invalidate_service()
            raise
        except Exception as e:
            root_error = str(e)
            raise
    if root_id is None:
        return {
            "uploaded": 0,
            "already_uploaded": already_uploaded,
            "skipped_missing": skipped_missing,
            "errors": [f"root folder: {root_error}"],
            "shadow_uploaded": shadow_uploaded,
            "shadow_skipped": shadow_skipped,
            "shadow_errors": shadow_errors,
        }

    total = len(to_upload)

    for i, (company, job_url, folder_path) in enumerate(to_upload, 1):
        if progress_cb and i % 5 == 0:
            await progress_cb(f"⏳ {i}/{total} uploaded…")

        async def _upload_row(fp: Path = folder_path) -> str:
            # _get_service() re-fetched here so a reauth rebuild is picked up.
            svc = _get_service()
            date_id = await asyncio.wait_for(
                _resolve_folder(svc, fp.parent.name, root_id),
                timeout=30,
            )
            company_id = await _drive_call(upload_folder, svc, fp, date_id, timeout=_UPLOAD_TIMEOUT)
            return folder_url(company_id)

        with best_effort("gdrive.upload_missing_folders"):
            try:
                drive_url = await _call_with_reauth(_upload_row)
                log.info("gdrive_sync: uploaded %s → %s", folder_path.name, drive_url)
                uploaded += 1
                if job_url:
                    await asyncio.to_thread(set_drive_url, job_url, drive_url)
            except asyncio.TimeoutError:
                # Same reasoning as the root-resolve timeout above: a worker
                # thread was abandoned holding the cached service's socket.
                msg = f"{company}: timeout after {_UPLOAD_TIMEOUT}s"
                errors.append(msg)
                log.warning("gdrive_sync: %s", msg)
                _invalidate_service()
                raise
            except Exception as e:
                errors.append(f"{company}: {e}")
                log.warning("gdrive_sync: upload failed for %s: %s", company, e)
                raise

    return {
        "uploaded": uploaded,
        "already_uploaded": already_uploaded,
        "skipped_missing": skipped_missing,
        "errors": errors,
        "shadow_uploaded": shadow_uploaded,
        "shadow_skipped": shadow_skipped,
        "shadow_errors": shadow_errors,
    }


async def _upload_shadow_subfolders(
    folders: set[Path],
    *,
    force: bool = False,
) -> tuple[int, int, list[str]]:
    """Upload any dual-apply shadow subfolder found under the given company folders.

    Shadow sets (``{company}/{shadow_profile_name}/``) have no tracker row, so
    they're invisible to the company-level Drive URL check in
    upload_missing_folders. This scans every locally-present company folder
    for a subdirectory matching a known LLM profile name and uploads it —
    idempotent (Drive upserts by name), so re-running is safe.

    Without a tracker row there is also no per-row "already uploaded" check,
    so without the ledger every shadow subfolder would be re-uploaded on
    EVERY pass forever (docs/GDRIVE_SSL_RACE_PLAN.md M3). `hunter.drive_ledger`
    tracks a content signature per folder path; a folder whose signature is
    unchanged since its last successful upload is skipped. `force=True`
    bypasses the ledger check (still records afterwards) — the escape hatch
    for a folder deleted on Drive by hand, which the ledger cannot see.

    Returns (uploaded, skipped, errors).
    """
    from hunter import drive_ledger
    from hunter.llm_profiles import PROFILES

    uploaded = 0
    skipped = 0
    errors: list[str] = []
    for folder_path in folders:
        for name in PROFILES:
            sub = folder_path / name
            if not sub.is_dir() or not any(f.is_file() for f in sub.iterdir()):
                continue
            path_key = str(sub)
            try:
                sig = drive_ledger.signature(sub)
            except Exception as e:
                errors.append(f"{folder_path.name}/{name}: signature failed: {e}")
                continue
            if not force and drive_ledger.is_current(path_key, sig):
                skipped += 1
                continue
            try:
                url = await upload_shadow_folder(folder_path, sub)
                if url:
                    uploaded += 1
                    drive_ledger.record(path_key, sig, url)
                else:
                    errors.append(f"{folder_path.name}/{name}: upload failed")
            except Exception as e:
                errors.append(f"{folder_path.name}/{name}: {e}")
    return uploaded, skipped, errors
