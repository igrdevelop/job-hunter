"""hunter/commands/google.py — Google Sheets, Google Drive, and About Me command handlers."""

import asyncio
import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def cmd_gsheets_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter import gsheets_sync
    try:
        report = await gsheets_sync.status_report()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Не удалось получить статус: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if not report["enabled"]:
        await update.message.reply_text(
            "ℹ️ <b>Google Sheets отключён</b> (GSHEETS_ENABLED=false).\n"
            "Задай GSHEETS_ENABLED=true в .env чтобы включить.",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = ["📊 <b>Google Sheets статус</b>"]
    lines.append(f"  Сервис: {'✅ OK' if report['service_ok'] else '❌ не инициализирован'}")
    if report.get("sheet_url"):
        lines.append(f"  Таблица: <a href=\"{report['sheet_url']}\">открыть</a>")
    elif report.get("sheet_id"):
        lines.append(f"  ID: <code>{report['sheet_id']}</code>")
    else:
        lines.append("  Таблица: не настроена")
    dirty = report.get("dirty_count", 0)
    lines.append(f"  Грязных строк: {dirty}")
    if dirty:
        lines.append("  ℹ️ Запусти /gsheets_resync для повторной отправки")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def cmd_gsheets_resync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter import gsheets_sync
    await update.message.reply_text("⏳ Повторная отправка грязных строк в Sheets…")
    try:
        synced = await gsheets_sync.resync_dirty()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.message.reply_text(
        f"✅ <b>gsheets_resync</b>: отправлено {synced} строк(и).",
        parse_mode=ParseMode.HTML,
    )


async def cmd_gsheets_push_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter import gsheets_sync
    await update.message.reply_text(
        "⏳ Ищу строки в tracker.xlsx, которых нет в Sheets…",
        parse_mode=ParseMode.HTML,
    )
    try:
        result = await gsheets_sync.push_missing_rows()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    pushed = result["pushed"]
    already = result["already_present"]
    errors = result.get("errors", [])
    err_note = f"\n⚠️ <code>{errors[0][:200]}</code>" if errors else ""
    await update.message.reply_text(
        f"✅ <b>gsheets_push_missing</b>\n"
        f"  📤 Добавлено: {pushed}\n"
        f"  ✔️ Уже были: {already}"
        f"{err_note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_gdrive_upload_missing(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        await update.message.reply_text(
            "⚠️ GDRIVE_ENABLED=false — Google Drive не активирован.",
            parse_mode=ParseMode.HTML,
        )
        return

    status_msg = await update.message.reply_text(
        "⏳ Загружаю папки из tracker.xlsx на Google Drive…",
        parse_mode=ParseMode.HTML,
    )

    async def _progress(text: str) -> None:
        try:
            await status_msg.edit_text(text, parse_mode=ParseMode.HTML)
        except Exception:
            pass

    try:
        from hunter import gdrive_sync
        result = await gdrive_sync.upload_missing_folders(PROJECT_DIR, progress_cb=_progress)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Ошибка: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    uploaded = result["uploaded"]
    skipped = result["skipped_missing"]
    errors = result.get("errors", [])
    err_note = ""
    if errors:
        err_lines = "\n".join(f"  • {e[:120]}" for e in errors[:5])
        err_note = f"\n⚠️ Ошибки ({len(errors)}):\n<code>{err_lines}</code>"
    await update.message.reply_text(
        f"✅ <b>gdrive_upload_missing</b>\n"
        f"  📤 Загружено: {uploaded}\n"
        f"  ⏭ Нет локально: {skipped}"
        f"{err_note}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_about_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate (or regenerate) About Me for a job URL in the tracker.

    Usage: /about_me <lang> <url>
    lang: en | pl
    """
    args = context.args or []
    if len(args) != 2:
        await update.message.reply_text(
            "Usage: /about_me <lang> <url>\nExample: /about_me pl https://justjoin.it/job-offer/..."
        )
        return

    lang, url = args[0].lower(), args[1]
    if lang not in ("en", "pl"):
        await update.message.reply_text("lang must be 'en' or 'pl'")
        return

    from hunter.tracker import get_folder_by_url, normalize_url
    from hunter.config import PROJECT_DIR

    normalized = normalize_url(url)
    folder_str = get_folder_by_url(normalized)
    if not folder_str:
        await update.message.reply_text(
            "URL not found in tracker. Run /force to process it first."
        )
        return

    folder_path = PROJECT_DIR / folder_str
    if not (folder_path / "job_posting.txt").exists():
        await update.message.reply_text(
            "No job_posting.txt in folder - cannot generate."
        )
        return

    await update.message.reply_text(f"⏳ Generating About Me ({lang.upper()})...")

    from hunter.about_me_agent import generate_about_me  # type: ignore[import]
    result = await asyncio.to_thread(generate_about_me, folder_path, lang)
    if not result:
        await update.message.reply_text("❌ Generation failed - check logs.")
        return

    await update.message.reply_text(result)
    await update.message.reply_text(
        f"✅ Saved to {folder_str}/About_Me_{lang.upper()}.txt"
    )
