"""commands/check_expired.py — /check_expired command handler."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import GSHEETS_ENABLED

logger = logging.getLogger(__name__)


async def cmd_check_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check all unsent tracker rows for expired job offers."""
    from hunter.expired_marker import run_check

    status_msg = await update.message.reply_text(
        "🔍 Checking tracker for expired vacancies…\n"
        "<i>EXPIRED will be written directly to tracker.xlsx.</i>",
        parse_mode=ParseMode.HTML,
    )

    async def progress_cb(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    try:
        result = await run_check(progress_cb=progress_cb)
    except Exception as e:
        logger.exception("[check_expired] Failed: %s", e)
        await status_msg.edit_text(
            f"❌ Error: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    total   = result["total"]
    alive   = result["alive"]
    expired = result["expired"]
    errors  = result["errors"]
    skipped = result.get("skipped", [])

    lines = [f"✅ <b>Check complete</b> — {total} vacancies\n"]

    if expired:
        lines.append(f"⏭ <b>Expired ({len(expired)}):</b>")
        for item in expired:
            lines.append(f"  • {item['company']} — {item['title']}")
        lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
        lines.append("")

    if errors:
        lines.append(f"⚠️ <b>Fetch errors ({len(errors)}):</b>")
        for item in errors[:5]:
            lines.append(f"  • {item['company']}: {item['error'][:60]}")
        if len(errors) > 5:
            lines.append(f"  … {len(errors) - 5} more")
        lines.append("")

    lines.append(f"✅ Alive: <b>{alive}</b>")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): <b>{len(skipped)}</b>")

    # Push EXPIRED stamps to Sheets — reads tracker.xlsx directly so it works
    # even after a bot restart (no dependency on the in-memory dirty cache).
    if GSHEETS_ENABLED and expired:
        try:
            from hunter import gsheets_sync
            pushed = await gsheets_sync.push_sent_column()
            if pushed["updated"]:
                lines.append(f"\n🔄 Sheets: {pushed['updated']} row(s) updated.")
        except Exception as e:
            logger.warning("[check_expired] gsheets push_sent_column failed: %s", e)

    await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
