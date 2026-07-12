"""commands/sync_sent.py — /sync_sent command handler."""

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import GSHEETS_ENABLED


async def cmd_sync_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Pull Sent/To Learn/Re-application changes from Google Sheets → tracker.xlsx."""
    if not GSHEETS_ENABLED:
        await update.message.reply_text(
            "ℹ️ Google Sheets disabled (GSHEETS_ENABLED=false). /sync_sent unavailable.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text("⏳ Syncing Sheets → tracker.xlsx…")
    try:
        from hunter import gsheets_sync

        result = await gsheets_sync.pull_full_snapshot()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Pull error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pulled = result["pulled"]
    inserted = result.get("inserted", 0)
    updated = result["updated"]
    errors = result["errors"]

    lines = ["✅ <b>sync_sent done</b>"]
    lines.append(f"  Rows from Sheets: {pulled}")
    if inserted:
        lines.append(f"  Inserted (self-heal): {inserted}")
    lines.append(f"  Updated in tracker.xlsx: {updated}")
    if errors:
        lines.append(f"⚠️ Errors: {'; '.join(errors[:2])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
