"""
hunter/gdrive_sync.py — High-level Google Drive upload logic.

Uploads application folders to Drive after a successful apply.
Best-effort: errors are logged as warnings, never propagated to caller.

Public API:
  upload_application_folder(folder_path) -> str | None
    Upload Applications/{date}/{company}/ to Drive.
    Returns folder URL or None if disabled / error.
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

    from hunter.gdrive_client import (
        get_or_create_folder,
        upload_folder,
        folder_url,
    )

    svc = _get_service()

    try:
        # folder_path is expected to be   .../Applications/{date}/{company}
        # We replicate that two-level structure under the root folder on Drive.
        company_name = folder_path.name
        date_name = folder_path.parent.name

        # 1. Root folder ("Job Hunter" or user-supplied ID)
        if GDRIVE_ROOT_FOLDER_ID:
            root_id = GDRIVE_ROOT_FOLDER_ID
        else:
            root_id = await asyncio.to_thread(
                get_or_create_folder, svc, GDRIVE_ROOT_FOLDER_NAME, None
            )

        # 2. Date sub-folder
        date_id = await asyncio.to_thread(
            get_or_create_folder, svc, date_name, root_id
        )

        # 3. Company sub-folder + upload files
        company_id = await asyncio.to_thread(
            upload_folder, svc, folder_path, date_id
        )

        url = folder_url(company_id)
        log.info("gdrive_sync: uploaded %s → %s", folder_path.name, url)
        return url

    except Exception as e:
        log.warning("gdrive_sync: upload failed for %s: %s", folder_path, e)
        return None
