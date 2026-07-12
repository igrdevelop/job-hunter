"""commands/unsent.py — /unsent command handler."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_unsent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Count unsent applications in tracker and how many have ANGULAR in stack."""
    try:
        from hunter.tracker_cache import cache

        if not cache.loaded:
            await cache.load_from_db()
        total = await cache.unsent_count()
        angular_n = await cache.unsent_angular_count()
        if total == 0:
            msg = "📭 <b>No unsent applications.</b>"
        else:
            msg = (
                f"📋 <b>Unsent applications:</b> {total}\n"
                f"🔷 <b>With ANGULAR in stack:</b> {angular_n}"
            )
    except Exception as exc:
        logger.exception("[unsent] Failed: %s", exc)
        msg = f"❌ Failed to read tracker: <code>{exc}</code>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
