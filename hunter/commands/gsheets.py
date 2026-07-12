"""commands/gsheets.py — /gsheets_status, /gsheets_push_missing, /gsheets_push_sent handlers."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import GSHEETS_ENABLED

logger = logging.getLogger(__name__)


async def cmd_gsheets_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show Google Sheets integration status."""
    from hunter import gsheets_sync

    try:
        report = await gsheets_sync.status_report()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to get status: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    enabled = report["enabled"]
    if not enabled:
        await update.message.reply_text(
            "ℹ️ <b>Google Sheets disabled</b> (GSHEETS_ENABLED=false).\n"
            "Set GSHEETS_ENABLED=true in .env to enable.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["📊 <b>Google Sheets status</b>"]
    lines.append(f"  Service: {'✅ OK' if report['service_ok'] else '❌ not initialized'}")
    if report.get("sheet_url"):
        lines.append(f'  Sheet: <a href="{report["sheet_url"]}">open</a>')
    elif report.get("sheet_id"):
        lines.append(f"  ID: <code>{report['sheet_id']}</code>")
    else:
        lines.append("  Sheet: not configured")
    dirty = report.get("dirty_count", 0)
    lines.append(f"  Dirty rows: {dirty}")
    if dirty:
        lines.append("  ℹ️ Run /gsheets_push_missing to resync")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_gsheets_push_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Push tracker.xlsx rows that are absent from Google Sheets (by ID)."""
    from hunter import gsheets_sync

    await update.message.reply_text(
        "⏳ Looking for tracker.xlsx rows absent from Sheets…",
        parse_mode=ParseMode.HTML,
    )
    try:
        result = await gsheets_sync.push_missing_rows()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pushed = result["pushed"]
    already = result["already_present"]
    errors = result.get("errors", [])
    err_note = f"\n⚠️ <code>{errors[0][:200]}</code>" if errors else ""
    await update.message.reply_text(
        f"✅ <b>gsheets_push_missing</b>\n"
        f"  📤 Pushed: {pushed}\n"
        f"  ✔️ Already present: {already}"
        f"{err_note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_gsheets_push_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Push Sent column from tracker.xlsx to Sheets for rows where they differ.

    Fixes EXPIRED/dates that are in tracker.xlsx but missing in Sheets —
    works even after bot restart (reads tracker.xlsx directly, no cache needed).
    """
    from hunter import gsheets_sync

    if not GSHEETS_ENABLED:
        await update.message.reply_text("ℹ️ Google Sheets disabled.")
        return

    await update.message.reply_text("⏳ Comparing Sent column: tracker.xlsx → Sheets…")
    try:
        result = await gsheets_sync.push_sent_column()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Error: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    checked = result["checked"]
    updated = result["updated"]
    errors = result["errors"]
    await update.message.reply_text(
        f"✅ <b>gsheets_push_sent</b>\n"
        f"  🔍 Rows with Sent in tracker: {checked}\n"
        f"  📤 Updated in Sheets: {updated}\n"
        f"  ⚠️ Errors: {errors}",
        parse_mode=ParseMode.HTML,
    )
