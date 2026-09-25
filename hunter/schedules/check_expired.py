"""schedules/check_expired.py — nightly expired check job callback.

`run_expired_check_and_report` is shared with the site's /pipeline control
bar (hunter/schedules/bot_commands.py, kind `check_expired`): same check,
same Telegram report — the web command just reports even when nothing
expired, since a human pressed the button and is waiting for an answer.
"""

import logging
from typing import Any

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID

logger = logging.getLogger(__name__)


async def run_expired_check_and_report(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    header: str = "🌙 <b>Nightly expired check</b>\n",
    report_when_nothing_expired: bool = False,
) -> dict[str, Any]:
    """Run hunter.expired_marker.run_check and send the Telegram summary.

    Raises whatever run_check raises — the caller decides how to report a
    failure. Returns run_check's result dict.
    """
    from hunter.expired_marker import run_check

    result = await run_check()

    expired = result["expired"]
    skipped = result.get("skipped", [])
    errors = result["errors"]

    if not expired and not report_when_nothing_expired:
        logger.info("[check_expired] Nothing expired.")
        return result

    lines = [header]
    lines.append(f"⏭ Expired: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Errors: {len(errors)}")
    if expired:
        lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )
    return result


async def scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, marks EXPIRED in tracker.xlsx."""
    logger.info("[scheduled_check_expired] Starting daily expired check")

    try:
        await run_expired_check_and_report(context)
    except Exception as e:
        logger.exception("[scheduled_check_expired] run_check failed: %s", e)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ <b>Scheduled check_expired failed</b>\n<pre>{str(e)[:300]}</pre>",
            parse_mode=ParseMode.HTML,
        )
