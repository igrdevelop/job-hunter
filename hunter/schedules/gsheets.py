"""schedules/gsheets.py — Google Sheets resync + pull job callbacks."""

import logging

from telegram.ext import ContextTypes

from hunter.config import TRACKER_PATH

logger = logging.getLogger(__name__)


async def scheduled_gsheets_resync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-5-min job: push dirty rows to Google Sheets (no-op if disabled)."""
    try:
        from hunter import gsheets_sync
        synced = await gsheets_sync.resync_dirty()
        if synced:
            logger.info("[scheduled_gsheets_resync] pushed %d dirty row(s)", synced)
    except Exception as e:
        logger.warning("[scheduled_gsheets_resync] failed: %s", e)


async def scheduled_gsheets_pull(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: pull Sheets → tracker.xlsx (every GSHEETS_REFRESH_INTERVAL_MIN)."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        from hunter import gsheets_sync
        result = await gsheets_sync.pull_full_snapshot()
        updated = result.get("updated", 0)
        if updated:
            logger.info("[scheduled_gsheets_pull] updated %d row(s) from Sheets", updated)
        if result.get("errors"):
            logger.warning("[scheduled_gsheets_pull] errors: %s", result["errors"])
    except Exception as e:
        logger.warning("[scheduled_gsheets_pull] failed: %s", e)
