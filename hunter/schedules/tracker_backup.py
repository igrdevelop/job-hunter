"""schedules/tracker_backup.py — daily tracker.xlsx backup job callback."""

import asyncio
import logging

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def scheduled_tracker_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily snapshot of tracker.xlsx (silent on success)."""
    try:
        from hunter.tracker_backup import run_tracker_backup

        result = await asyncio.to_thread(run_tracker_backup)
        if not result.get("ok") or result.get("errors"):
            err = "; ".join(result.get("errors") or [])[:400]
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{err}</pre>",
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.exception("[tracker_backup] scheduled job failed: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{str(exc)[:400]}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
