"""commands/normalize.py — /normalize: rebuild column L "Applied Date" from Sent."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_normalize(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse the Sent column into the clean date column L on Google Sheets."""
    from hunter.sent_normalizer import APPLIED_COL, APPLIED_HEADER, normalize_sheet_async

    await update.message.reply_text("⏳ Normalizing Sent → clean date column…")
    try:
        result = await normalize_sheet_async()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not result.get("enabled"):
        await update.message.reply_text("ℹ️ Google Sheets disabled or not configured.")
        return

    await update.message.reply_text(
        f"✅ <b>Column {APPLIED_COL} ({APPLIED_HEADER}) refreshed</b>\n"
        f"  📅 Applications with a date: {result.get('filled', 0)}\n"
        f"  📄 Rows scanned: {result.get('rows', 0)}",
        parse_mode=ParseMode.HTML,
    )
