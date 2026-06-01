"""schedules/gdrive.py — periodic Google Drive upload job callbacks."""

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


async def scheduled_gdrive_upload_logs(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job: upload hunter_errors.log to Google Drive Logs/ folder (no-op if disabled).

    Fires at 06:10 (after the nightly tracker backup at 06:05).
    Overwrites the previous log file so the Logs/ folder stays tidy.
    """
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        return
    try:
        from hunter import gdrive_sync
        log_path = PROJECT_DIR / "logs" / "hunter_errors.log"
        url = await gdrive_sync.upload_log_file(log_path)
        if url:
            logger.info("[scheduled_gdrive_upload_logs] uploaded → %s", url)
        else:
            logger.debug("[scheduled_gdrive_upload_logs] nothing uploaded (disabled or file missing)")
    except Exception as e:
        logger.warning("[scheduled_gdrive_upload_logs] failed: %s", e)
