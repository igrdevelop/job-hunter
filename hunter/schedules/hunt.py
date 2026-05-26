"""schedules/hunt.py — scheduled hunt job callback."""

import logging

from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID
from telegram.constants import ParseMode

logger = logging.getLogger(__name__)


async def scheduled_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Staggered per-source scheduled hunt job."""
    from hunter.main import run_hunt

    source_names = context.job.data.get("source_names") if context.job.data else None
    try:
        await run_hunt(context, source_names=source_names)
    except Exception as e:
        label = ", ".join(source_names) if source_names else "all"
        logger.exception(f"[scheduled_hunt] Unhandled error for {label}")
        extra = ""
        if "Content_Types" in str(e) or "archive" in str(e).lower():
            extra = (
                "\n\n<i>Likely corrupt or non-xlsx tracker.xlsx file — "
                "not a board error in parentheses.</i>"
            )
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Hunt error</b> ({label}):\n<pre>{str(e)[:500]}</pre>{extra}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
