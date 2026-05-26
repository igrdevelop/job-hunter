"""schedules/daily_summary.py — daily applications summary job callback."""

import asyncio
import logging

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job at 00:01: send a summary of how many applications were made yesterday."""
    from datetime import date as _date, timedelta
    from hunter.tracker import get_applications_on_date
    from hunter.bot.formatters import _format_daily_summary

    yesterday = (_date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        apps = await asyncio.to_thread(get_applications_on_date, yesterday)
    except Exception as e:
        logger.warning("[scheduled_daily_summary] failed to read tracker: %s", e)
        return

    if not apps:
        return  # silent when nothing was applied to yesterday

    text = _format_daily_summary(apps, yesterday)
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_daily_summary] send failed: %s", e)
