"""schedules/gdrive.py — periodic Google Drive upload job callback."""

import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def scheduled_gdrive_upload_missing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-3-hour job: upload application folders missing from Google Drive (no-op if disabled)."""
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        return
    try:
        from hunter import gdrive_sync
        result = await gdrive_sync.upload_missing_folders(PROJECT_DIR)
        uploaded = result.get("uploaded", 0)
        if uploaded:
            logger.info("[scheduled_gdrive_upload_missing] uploaded %d folder(s)", uploaded)
    except Exception as e:
        logger.warning("[scheduled_gdrive_upload_missing] failed: %s", e)
