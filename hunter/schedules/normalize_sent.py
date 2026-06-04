"""schedules/normalize_sent.py — daily Sent → clean date (column L) refresh."""

import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def scheduled_normalize_sent(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job: rebuild column L "Applied Date" from the Sent column (no-op if disabled)."""
    try:
        from hunter.sent_normalizer import normalize_sheet_async
        result = await normalize_sheet_async()
        if result.get("enabled"):
            logger.info(
                "[scheduled_normalize_sent] column L refreshed: %d/%d rows have a date",
                result.get("filled", 0), result.get("rows", 0),
            )
    except Exception as e:
        logger.warning("[scheduled_normalize_sent] failed: %s", e)
