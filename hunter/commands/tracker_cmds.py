"""hunter/commands/tracker_cmds.py — Tracker-related command handlers."""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_check_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.expired_marker import run_check

    status_msg = await update.message.reply_text(
        "🔍 Проверяю трекер на истёкшие вакансии...\n"
        "<i>EXPIRED будет записан прямо в tracker.xlsx.</i>",
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
            f"❌ Ошибка: <code>{str(e)[:200]}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    total   = result["total"]
    alive   = result["alive"]
    expired = result["expired"]
    errors  = result["errors"]
    skipped = result.get("skipped", [])

    lines = [f"✅ <b>Проверка завершена</b> — {total} вакансий\n"]

    if expired:
        lines.append(f"⏭ <b>Истекло ({len(expired)}):</b>")
        for item in expired:
            lines.append(f"  • {item['company']} — {item['title']}")
        lines.append(f"\n📊 tracker.xlsx обновлён — {len(expired)} строк(и) помечено EXPIRED.")
        lines.append("")

    if errors:
        lines.append(f"⚠️ <b>Ошибки загрузки ({len(errors)}):</b>")
        for item in errors[:5]:
            lines.append(f"  • {item['company']}: {item['error'][:60]}")
        if len(errors) > 5:
            lines.append(f"  … ещё {len(errors) - 5}")
        lines.append("")

    lines.append(f"✅ Живых: <b>{alive}</b>")
    if skipped:
        lines.append(f"⏩ Пропущено (jobleads): <b>{len(skipped)}</b>")

    await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_sync_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter import gsheets_sync
    from hunter.config import GSHEETS_ENABLED

    if not GSHEETS_ENABLED:
        await update.message.reply_text(
            "ℹ️ Google Sheets отключён (GSHEETS_ENABLED=false). /sync_sent недоступен.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text("⏳ Синхронизирую Sheets → tracker.xlsx…")
    try:
        result = await gsheets_sync.pull_full_snapshot()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка pull: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pulled = result["pulled"]
    updated = result["updated"]
    errors = result["errors"]

    lines = ["✅ <b>sync_sent завершён</b>"]
    lines.append(f"  Строк из Sheets: {pulled}")
    lines.append(f"  Обновлено в tracker.xlsx: {updated}")
    if errors:
        lines.append(f"⚠️ Ошибки: {'; '.join(errors[:2])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
