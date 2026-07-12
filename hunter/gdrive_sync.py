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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def _do_upload(folder_path: Path) -> str:
    """Core upload logic — raises on error. Called by both public functions."""
    from hunter.gdrive_client import get_or_create_folder, upload_folder, folder_url

    svc = _get_service()
    date_name = folder_path.parent.name

    if GDRIVE_ROOT_FOLDER_ID:
        root_id = GDRIVE_ROOT_FOLDER_ID
    else:
        root_id = await asyncio.to_thread(get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None)

    date_id = await asyncio.to_thread(get_or_create_folder, svc, date_name, root_id)
    company_id = await asyncio.to_thread(upload_folder, svc, folder_path, date_id)
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

    try:
        url = await _do_upload(folder_path)
        log.info("gdrive_sync: uploaded %s → %s", folder_path.name, url)
        if job_url:
            from hunter.tracker import set_drive_url

            await asyncio.to_thread(set_drive_url, job_url, url)
        return url
    except Exception as e:
        log.warning("gdrive_sync: upload failed for %s: %s", folder_path, e)
        return None


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

    try:
        from hunter.gdrive_client import get_or_create_folder, upload_folder, folder_url

        svc = _get_service()
        date_name = primary_folder.parent.name

        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await asyncio.to_thread(
                get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None
            )
        date_id = await asyncio.to_thread(get_or_create_folder, svc, date_name, root_id)
        company_id = await asyncio.to_thread(
            get_or_create_folder, svc, primary_folder.name, date_id
        )
        shadow_id = await asyncio.to_thread(upload_folder, svc, shadow_subfolder, company_id)
        url = folder_url(shadow_id)
        log.info("gdrive_sync: uploaded shadow %s → %s", shadow_subfolder, url)
        return url
    except Exception as e:
        log.warning("gdrive_sync: shadow upload failed for %s: %s", shadow_subfolder, e)
        return None


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

        from hunter.gdrive_client import get_or_create_folder, upload_file

        svc = _get_service()

        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await asyncio.to_thread(
                get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None
            )

        logs_folder_id = await asyncio.to_thread(get_or_create_folder, svc, "Logs", root_id)
        file_id = await asyncio.to_thread(upload_file, svc, dated_file, logs_folder_id)
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


async def upload_missing_folders(
    project_dir: Path,
    progress_cb=None,
) -> dict:
    """Upload tracker.xlsx application folders that haven't been uploaded to Drive yet.

    Skips rows that already have a Drive URL in col 12. After each successful
    upload, writes the Drive URL back to tracker so the row is not re-uploaded.

    progress_cb: optional async callable(str) for Telegram progress updates.

    Returns:
      {"uploaded": int, "already_uploaded": int, "skipped_missing": int, "errors": list[str]}
    """
    if not _ready():
        return {
            "uploaded": 0,
            "already_uploaded": 0,
            "skipped_missing": 0,
            "errors": ["GDRIVE_ENABLED is false or service not ready"],
            "shadow_uploaded": 0,
            "shadow_errors": [],
        }

    from hunter.gdrive_client import get_or_create_folder, upload_folder, folder_url
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

    shadow_uploaded, shadow_errors = await _upload_shadow_subfolders(existing_folders)

    if not to_upload:
        return {
            "uploaded": 0,
            "already_uploaded": already_uploaded,
            "skipped_missing": skipped_missing,
            "errors": [],
            "shadow_uploaded": shadow_uploaded,
            "shadow_errors": shadow_errors,
        }

    svc = _get_service()
    errors: list[str] = []
    uploaded = 0

    # Resolve root folder once — avoids a redundant API call per row.
    try:
        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await asyncio.wait_for(
                asyncio.to_thread(get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None),
                timeout=30,
            )
    except Exception as e:
        return {
            "uploaded": 0,
            "already_uploaded": already_uploaded,
            "skipped_missing": skipped_missing,
            "errors": [f"root folder: {e}"],
            "shadow_uploaded": shadow_uploaded,
            "shadow_errors": shadow_errors,
        }

    total = len(to_upload)

    for i, (company, job_url, folder_path) in enumerate(to_upload, 1):
        if progress_cb and i % 5 == 0:
            await progress_cb(f"⏳ {i}/{total} uploaded…")
        try:
            date_name = folder_path.parent.name
            date_id = await asyncio.wait_for(
                asyncio.to_thread(get_or_create_folder, svc, date_name, root_id),
                timeout=30,
            )
            company_id = await asyncio.wait_for(
                asyncio.to_thread(upload_folder, svc, folder_path, date_id),
                timeout=_UPLOAD_TIMEOUT,
            )
            drive_url = folder_url(company_id)
            log.info("gdrive_sync: uploaded %s → %s", folder_path.name, drive_url)
            uploaded += 1
            if job_url:
                await asyncio.to_thread(set_drive_url, job_url, drive_url)
        except asyncio.TimeoutError:
            msg = f"{company}: timeout after {_UPLOAD_TIMEOUT}s"
            errors.append(msg)
            log.warning("gdrive_sync: %s", msg)
        except Exception as e:
            errors.append(f"{company}: {e}")
            log.warning("gdrive_sync: upload failed for %s: %s", company, e)

    return {
        "uploaded": uploaded,
        "already_uploaded": already_uploaded,
        "skipped_missing": skipped_missing,
        "errors": errors,
        "shadow_uploaded": shadow_uploaded,
        "shadow_errors": shadow_errors,
    }


async def _upload_shadow_subfolders(folders: set[Path]) -> tuple[int, list[str]]:
    """Upload any dual-apply shadow subfolder found under the given company folders.

    Shadow sets (``{company}/{shadow_profile_name}/``) have no tracker row, so
    they're invisible to the company-level Drive URL check in
    upload_missing_folders. This scans every locally-present company folder
    for a subdirectory matching a known LLM profile name and uploads it —
    idempotent (Drive upserts by name), so re-running is safe.
    """
    from hunter.llm_profiles import PROFILES

    uploaded = 0
    errors: list[str] = []
    for folder_path in folders:
        for name in PROFILES:
            sub = folder_path / name
            if not sub.is_dir() or not any(f.is_file() for f in sub.iterdir()):
                continue
            try:
                url = await upload_shadow_folder(folder_path, sub)
                if url:
                    uploaded += 1
                else:
                    errors.append(f"{folder_path.name}/{name}: upload failed")
            except Exception as e:
                errors.append(f"{folder_path.name}/{name}: {e}")
    return uploaded, errors
