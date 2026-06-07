"""schedules/gsheets.py — Google Sheets resync + pull job callbacks."""

import logging

from telegram.ext import ContextTypes

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
            await cache.load_from_db()
        from hunter import gsheets_sync
        result = await gsheets_sync.pull_full_snapshot()
        updated = result.get("updated", 0)
        inserted = result.get("inserted", 0)
        reconciled = result.get("reconciled", 0)
        if updated or inserted or reconciled:
            logger.info(
                "[scheduled_gsheets_pull] %d updated, %d inserted, %d reconciled from Sheets",
                updated, inserted, reconciled,
            )
            # Pull wrote directly to the DB; refresh the in-memory cache so /unsent,
            # /status and dedup reflect the new state without a bot restart.
            await cache.load_from_db()
        if result.get("errors"):
            logger.warning("[scheduled_gsheets_pull] errors: %s", result["errors"])
    except Exception as e:
        logger.warning("[scheduled_gsheets_pull] failed: %s", e)
