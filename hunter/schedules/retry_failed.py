"""schedules/retry_failed.py — scheduled retry of FAILed tracker rows.

Split out of the hunt tail (docs/HUNT_QUEUE_AND_DELIVERY_PLAN.md M2): retrying
the global FAIL list after every per-source hunt kept _hunt_lock busy past the
40-min slot spacing and hammered the same list dozens of times a day. Now it
runs only at RETRY_FAILED_TIMES (default 07:45 / 18:45 Warsaw).
"""

import logging

from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def scheduled_retry_failed(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry previously FAILed jobs on their own schedule."""
    from hunter.main import run_retry_failed

    try:
        await run_retry_failed(context)
    except Exception as e:
        logger.exception("[scheduled_retry_failed] Unhandled error")
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Retry error</b>:\n<pre>{str(e)[:500]}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
