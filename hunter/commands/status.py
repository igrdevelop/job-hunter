"""hunter/commands/status.py — Status and informational command handlers."""

import asyncio
import logging
from collections import Counter
from datetime import date, timedelta

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from hunter.config import (
    SCHEDULE_SOURCE_OFFSET_MIN,
    SCHEDULE_TIMES,
    TIMEZONE,
    TRACKER_PATH,
)

logger = logging.getLogger(__name__)

_REACT_SENT_MARKERS = {"—", "–", "-"}


def _build_schedule_text() -> str:
    from hunter.sources import ALL_SOURCES

    lines = []
    for idx, source in enumerate(ALL_SOURCES):
        times = []
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            times.append(f"{total // 60:02d}:{total % 60:02d}")
        lines.append(f"  <b>{source.name}</b>: {' / '.join(times)}")

    schedule_str = "\n".join(lines)
    return (
        f"⏰ <b>Расписание</b> ({TIMEZONE}, интервал {SCHEDULE_SOURCE_OFFSET_MIN} мин):\n"
        f"{schedule_str}"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>Job Hunter Bot</b>\n\n"
        "Commands:\n"
        "/hunt [source …] - run search (all sources, or e.g. <code>/hunt arbeitnow justjoin</code>)\n"
        "/schedule - show source schedule\n"
        "/status - show schedule + bot status\n"
        "/stats - статистика за последние 30 дней\n"
        "/force — принудительная генерация: <code>/force URL</code> или <code>/force</code> "
        "+ длинный текст вакансии (обход дедупа и React-only; JobLeads: "
        "<code>job_posting.txt</code>)\n"
        "/process_manual - process MANUAL rows with filled job_posting.txt\n"
        "/sync_sent - sync Sent column from Google Sheets → tracker.xlsx\n"
        "/unsent - сколько неотосланных заявок и сколько с ANGULAR\n"
        "/check_expired - проверить трекер на истёкшие вакансии\n"
        "/gsheets_status - статус интеграции Google Sheets\n"
        "/gsheets_resync - повторно отправить «грязные» строки в Sheets\n"
        "/gsheets_push_missing - добавить в Sheets строки из tracker.xlsx которых там нет\n"
        "/gdrive_upload_missing - загрузить все папки из tracker.xlsx на Google Drive\n\n"
        "Or just send a job URL to generate docs.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show 30-day hunt statistics from tracker.xlsx."""
    from hunter.tracker import read_all_tracker_rows

    rows = await asyncio.to_thread(read_all_tracker_rows)
    cutoff = (date.today() - timedelta(days=30)).isoformat()

    applied_scores: list[float] = []
    counts: Counter[str] = Counter()
    company_counter: Counter[str] = Counter()

    for row in rows:
        row_date = row.get("Date", "")[:10]
        if len(row_date) < 10 or row_date < cutoff:
            continue
        ats = row.get("ATS %", "").strip()
        if not ats or ats in ("—", "-", "–"):
            continue
        sent = row.get("Sent", "").strip()
        try:
            score = float(ats)
            counts["applied"] += 1
            applied_scores.append(score)
            company = row.get("Company", "").strip()
            if company:
                company_counter[company] += 1
        except ValueError:
            status = ats.upper()
            if status == "SKIP" and sent in _REACT_SENT_MARKERS:
                counts["react"] += 1
            else:
                counts[status] += 1

    total = sum(counts.values())
    avg = f"{sum(applied_scores) / len(applied_scores):.0f}%" if applied_scores else "—"
    top = company_counter.most_common(5)
    top_line = ", ".join(f"{c} ({n})" for c, n in top) if top else "—"

    lines = [
        "📊 <b>Job Hunt Stats (last 30 days)</b>",
        f"  Applied:     <b>{counts['applied']}</b>  (avg ATS: {avg})",
        f"  Skipped:     <b>{counts.get('SKIP', 0)}</b>",
        f"  React-only:  <b>{counts.get('react', 0)}</b>",
        f"  Expired:     <b>{counts.get('EXPIRED', 0)}</b>",
        f"  Failed:      <b>{counts.get('FAIL', 0)}</b>",
        f"  Total rows:  {total}",
        f"\n🏢 Top companies: {top_line}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import _hunt_lock
    from hunter.config import AUTO_APPLY
    from hunter.telegram_bot import _active_apply_urls, _pending_jobs

    pending = len(_pending_jobs)
    lock_status = "🔒 Auto-apply in progress" if _hunt_lock.locked() else "🔓 Idle"
    mode = "AUTO" if AUTO_APPLY else "MANUAL"

    active_lines = ""
    if _active_apply_urls:
        urls_list = "\n".join(f"  • {u}" for u in sorted(_active_apply_urls))
        active_lines = f"\n⚙️ Generating ({len(_active_apply_urls)}):\n{urls_list}"

    schedule_str = _build_schedule_text()
    await update.message.reply_text(
        f"{schedule_str}\n\n"
        f"🔧 Mode: {mode}\n"
        f"{lock_status}\n"
        f"📋 Pending decisions: {pending} jobs"
        f"{active_lines}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(_build_schedule_text(), parse_mode=ParseMode.HTML)


async def cmd_unsent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сколько неотосланных заявок в трекере и сколько с ANGULAR в Stack."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        angular_n = await cache.unsent_angular_count()
        if total == 0:
            msg = "📭 <b>Неотосланных заявок нет.</b>"
        else:
            msg = (
                f"📋 <b>Неотосланных заявок:</b> {total}\n"
                f"🔷 <b>С ANGULAR в Stack:</b> {angular_n}"
            )
    except Exception as exc:
        logger.exception("[unsent] Failed: %s", exc)
        msg = f"❌ Не удалось прочитать трекер: <code>{exc}</code>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Regenerate tracker.xlsx from SQLite and report row count."""
    await update.message.reply_text("⏳ Экспортирую tracker.xlsx из SQLite…")
    try:
        from hunter.tracker import export_to_excel
        n = await asyncio.to_thread(export_to_excel)
        await update.message.reply_text(
            f"✅ <b>tracker.xlsx обновлён</b> — {n} строк экспортировано из SQLite.",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        logger.exception("[export] Failed: %s", exc)
        await update.message.reply_text(
            f"❌ Экспорт не удался: <code>{exc}</code>",
            parse_mode=ParseMode.HTML,
        )
