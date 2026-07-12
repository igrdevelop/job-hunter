"""commands/check_responses.py — /check_responses command handler."""

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import EMAIL_RESPONSE_LOOKBACK_DAYS

logger = logging.getLogger(__name__)


async def cmd_check_responses(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check Gmail for application confirmation emails and update tracker.

    Usage: /check_responses [days]
      days — how many days back to scan (default: EMAIL_RESPONSE_LOOKBACK_DAYS = 2).
      Example: /check_responses 60  — backfill last 60 days.
    """
    from hunter.bot.formatters import _format_check_responses_report

    days: int | None = None
    if context.args:
        try:
            days = int(context.args[0])
            if days < 1:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                f"⚠️ Invalid argument: <code>{context.args[0]}</code>\n"
                "Usage: <code>/check_responses [days]</code>",
                parse_mode=ParseMode.HTML,
            )
            return

    days_label = days if days is not None else EMAIL_RESPONSE_LOOKBACK_DAYS
    status_msg = await update.message.reply_text(
        f"📬 Checking Gmail for confirmation emails (last {days_label} day(s))…",
        parse_mode=ParseMode.HTML,
    )
    try:
        from hunter.email_response_checker import run_confirmation_check

        results = await asyncio.to_thread(run_confirmation_check, days)
    except FileNotFoundError:
        await status_msg.edit_text(
            "❌ <b>Gmail not configured.</b>\n"
            "Run <code>python tools/gmail_auth.py</code> to set up access.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.exception("[check_responses] Failed: %s", e)
        await status_msg.edit_text(
            f"❌ Error: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    report = _format_check_responses_report(results)
    await status_msg.edit_text(report, parse_mode=ParseMode.HTML)
