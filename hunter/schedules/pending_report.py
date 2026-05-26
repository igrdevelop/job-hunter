"""schedules/pending_report.py — twice-daily pending applications report job callback."""

import logging

from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from hunter.config import TELEGRAM_CHAT_ID, TRACKER_PATH

logger = logging.getLogger(__name__)


async def scheduled_pending_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: report how many unsent applications are in tracker."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        if total == 0:
            msg = "📭 <b>No unsent applications.</b>"
        else:
            rows = await cache.all_unsent()
            fail_n = sum(1 for r in rows if r.get("ATS %") == "FAIL")
            manual_n = sum(1 for r in rows if r.get("ATS %") == "MANUAL")
            ready_n = total - fail_n - manual_n
            parts = [f"📋 <b>Unsent applications: {total}</b>"]
            if ready_n:
                parts.append(f"  ✅ Ready to send: {ready_n}")
            if manual_n:
                parts.append(f"  📝 MANUAL (text needed): {manual_n}")
            if fail_n:
                parts.append(f"  ❌ FAIL: {fail_n}")
            msg = "\n".join(parts)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=msg,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("[scheduled_pending_report] Failed")
