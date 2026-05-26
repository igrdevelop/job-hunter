"""schedules/email_responses.py — daily email confirmation check job callback."""

import asyncio
import logging

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def scheduled_check_email_responses(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check Gmail for new confirmation emails; notify only if new ones found."""
    try:
        from hunter.email_response_checker import run_confirmation_check
        results = await asyncio.to_thread(run_confirmation_check)
    except FileNotFoundError:
        logger.debug("[scheduled_check_email_responses] gmail_token.json missing — skipping")
        return
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] failed: %s", e)
        return

    # Only notify about rows that were just written (response was empty before this run)
    newly_confirmed = [
        r for r in results
        if r.match_type in ("exact", "fuzzy")
        and r.row_num is not None
        and not r.candidates[0].get("confirmation", "")
    ]
    if not newly_confirmed:
        return

    lines = [f"📬 <b>New confirmations from email ({len(newly_confirmed)}):</b>"]
    for r in newly_confirmed:
        c = r.candidates[0]
        lines.append(f"  • <b>{c['company']}</b> — {c['title']} <i>[{r.email.platform}]</i>")

    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] send failed: %s", e)
