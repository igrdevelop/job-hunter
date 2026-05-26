"""schedules/check_expired.py — nightly expired check job callback."""

import logging

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, marks EXPIRED in tracker.xlsx."""
    from hunter.expired_marker import run_check

    logger.info("[scheduled_check_expired] Starting daily expired check")

    try:
        result = await run_check()
    except Exception as e:
        logger.exception("[scheduled_check_expired] run_check failed: %s", e)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ <b>Scheduled check_expired failed</b>\n<pre>{str(e)[:300]}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    expired = result["expired"]
    skipped = result.get("skipped", [])
    errors  = result["errors"]

    if not expired:
        logger.info("[scheduled_check_expired] Nothing expired.")
        return

    lines = [f"🌙 <b>Nightly expired check</b>\n"]
    lines.append(f"⏭ Expired: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Errors: {len(errors)}")
    lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )
