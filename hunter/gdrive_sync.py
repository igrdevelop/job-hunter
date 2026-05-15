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
        root_id = await asyncio.to_thread(
            get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None
        )

    date_id = await asyncio.to_thread(get_or_create_folder, svc, date_name, root_id)
    company_id = await asyncio.to_thread(upload_folder, svc, folder_path, date_id)
    return folder_url(company_id)


async def upload_application_folder(folder_path: Path) -> str | None:
    """
    Upload Applications/{date}/{company}/ to Google Drive.

    Drive structure created:
      Job Hunter (or GDRIVE_ROOT_FOLDER_NAME) /
        {date} /
          {company} /
            <all files>

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
        return url
    except Exception as e:
        log.warning("gdrive_sync: upload failed for %s: %s", folder_path, e)
        return None


async def upload_missing_folders(project_dir: Path) -> dict:
    """Upload all tracker.xlsx application folders to Drive.

    Reads every row with a non-empty Folder column, resolves the path relative
    to project_dir, and uploads it via upload_application_folder().
    Already-uploaded files are overwritten idempotently (Drive deduplicates by name).

    Returns:
      {"uploaded": int, "skipped_missing": int, "errors": list[str]}
    """
    if not _ready():
        return {"uploaded": 0, "skipped_missing": 0, "errors": ["GDRIVE_ENABLED is false or service not ready"]}

    from hunter.tracker import read_all_tracker_rows

    rows = await asyncio.to_thread(read_all_tracker_rows)

    uploaded = 0
    skipped_missing = 0
    errors: list[str] = []

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

        company = row.get("Company", folder_path.name)
        try:
            url = await _do_upload(folder_path)
            log.info("gdrive_sync: uploaded %s → %s", folder_path.name, url)
            uploaded += 1
        except Exception as e:
            errors.append(f"{company}: {e}")

    return {"uploaded": uploaded, "skipped_missing": skipped_missing, "errors": errors}
