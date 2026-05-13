"""
telegram_bot.py — Telegram bot: notifications, inline buttons, callback handlers.

Pending jobs are stored in memory (dict job_id → Job) per session.
If the bot restarts, old buttons become "expired" — that's acceptable.
"""

import asyncio
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hunter.config import (
    APPLY_AGENT_PATH,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    SCHEDULE_TIMES,
    SCHEDULE_SOURCE_OFFSET_MIN,
    TRACKER_BACKUP_ENABLED,
    TRACKER_BACKUP_TIME,
    EXPIRED_CHECK_TIME,
)
from hunter.models import Job
from hunter.tracker import (
    add_skipped,

    lookup_url,
    lookup_company,
    manual_jobleads_job_posting_path,
    normalize_url,
)

logger = logging.getLogger(__name__)

# In-memory store: job_id (10-char hash) → Job
# Cleared on bot restart — acceptable trade-off vs complexity of persistence
_pending_jobs: dict[str, Job] = {}


# ── Keyboard factory ──────────────────────────────────────────────────────────

def _make_keyboard(job_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Apply", callback_data=f"apply:{job_id}"),
        InlineKeyboardButton("❌ Skip",  callback_data=f"skip:{job_id}"),
    ]])


# ── Public API (called from main.py) ─────────────────────────────────────────

async def send_text(context: ContextTypes.DEFAULT_TYPE, text: str) -> None:
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text=text,
        parse_mode=ParseMode.HTML,
    )


async def send_job_cards(context: ContextTypes.DEFAULT_TYPE, jobs: list[Job]) -> None:
    """Send one Telegram message per job with Apply/Skip buttons."""
    for job in jobs:
        jid = job.job_id()
        _pending_jobs[jid] = job
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=job.telegram_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_make_keyboard(jid),
            disable_web_page_preview=True,
        )


# ── Command handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "🤖 <b>Job Hunter Bot</b>\n\n"
        "Commands:\n"
        "/hunt [source …] - run search (all sources, or e.g. <code>/hunt arbeitnow justjoin</code>)\n"
        "/schedule - show source schedule\n"
        "/status - show schedule + bot status\n"
        "/force — принудительная генерация: <code>/force URL</code> или <code>/force</code> "
        "+ длинный текст вакансии (обход дедупа и React-only; JobLeads: "
        "<code>job_posting.txt</code>)\n"
        "/process_manual - process MANUAL rows with filled job_posting.txt\n"
        "/sync_sent - sync Sent column from to_send.xlsx → tracker.xlsx\n"
        "/unsent - сколько неотосланных в to_send и сколько с ANGULAR в Stack\n"
        "/check_expired - проверить to_send на истёкшие вакансии (работает с копией)\n"
        "/apply_expired - применить результаты проверки к to_send.xlsx\n\n"
        "Or just send a job URL to generate docs.",
        parse_mode=ParseMode.HTML,
    )


def _parse_hunt_source_args(args: list[str], valid_names: set[str]) -> tuple[list[str] | None, list[str]]:
    """Split /hunt arguments into source slugs. Returns (names or None for «all», unknown slugs)."""
    requested: list[str] = []
    for a in args:
        for part in a.split(","):
            part = part.strip().lower()
            if part:
                requested.append(part)
    if not requested:
        return None, []
    seen: set[str] = set()
    unique: list[str] = []
    for r in requested:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    unknown = [r for r in unique if r not in valid_names]
    if unknown:
        return [], unknown
    return unique, []


async def cmd_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger — full hunt or a subset of sources (same names as in /schedule)."""
    from hunter.main import run_hunt
    from hunter.sources import ALL_SOURCES

    valid_names = {s.name for s in ALL_SOURCES}
    source_names, unknown = _parse_hunt_source_args(context.args or [], valid_names)

    if unknown:
        avail = ", ".join(sorted(valid_names))
        await update.message.reply_text(
            f"❌ Unknown source(s): <b>{', '.join(unknown)}</b>\n\n"
            f"Available: <code>{avail}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if source_names:
        label = ", ".join(source_names)
        await update.message.reply_text(
            f"🔍 Running hunt: <b>{label}</b>",
            parse_mode=ParseMode.HTML,
        )
        await run_hunt(context, source_names=source_names)
    else:
        await update.message.reply_text("🔍 Running hunt (all sources)...")
        await run_hunt(context)


async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-process: URL from tracker / React-only, or full pasted posting after /force."""
    raw = (update.message.text or "").strip()
    m = re.match(r"/force(?:@\w+)?\s*(.*)\Z", raw, flags=re.DOTALL | re.IGNORECASE)
    body = (m.group(1) or "").strip() if m else ""

    if not body:
        await update.message.reply_text(
            "<b>/force</b> — принудительная генерация (<code>--force</code>):\n\n"
            "• <code>/force https://…</code> — по ссылке\n"
            "• <code>/force</code> и с новой строки (или через пробел) полный текст вакансии — "
            "как обычная вставка, но с обходом дедупа и React-only\n\n"
            "Текст должен быть достаточно длинным (как при вставке JD), иначе пришли http-ссылку.",
            parse_mode=ParseMode.HTML,
        )
        return

    if _looks_like_paste(body):
        await update.message.reply_text(
            f"🔧 <b>Force + текст вакансии</b> — {len(body.strip())} симв. "
            "Обход: дедуп трекера, React-only. Запускаю…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] paste mode ({len(body)} chars)")
        await _handle_paste(update, body, force=True)
        return

    if body.startswith("http"):
        url = _extract_url(body) or body.split()[0].strip()
        await update.message.reply_text(
            f"⏳ <b>Force: запускаю генерацию</b> (<code>--force</code>)\n"
            f"🔗 {url}\n\n"
            "Обход: дедуп трекера, React-only skip; для JobLeads — вставленный "
            "<code>job_posting.txt</code> подставится при fetch.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[Force] Launching apply_agent --force for: {url}")
        asyncio.create_task(_run_apply_agent(url, force=True))
        return

    await update.message.reply_text(
        "После <code>/force</code> нужна <b>http(s)-ссылка</b> или длинный текст вакансии "
        "(как при обычной вставке). Одно слово без ссылки не подходит.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_process_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Process all MANUAL-pending tracker rows whose job_posting.txt is already filled."""
    from hunter.tracker import get_all_manual_pending
    from job_fetch.jobleads import try_load_manual_job_posting

    rows = await asyncio.to_thread(get_all_manual_pending)
    if not rows:
        await update.message.reply_text("✅ Нет MANUAL вакансий для обработки.")
        return

    ready = []
    for row in rows:
        content = await asyncio.to_thread(try_load_manual_job_posting, row["url"])
        if content:
            ready.append(row)

    if not ready:
        lines = [
            f"  Row {r['row']}: <b>{r['company']}</b> - {r['title']}"
            + (f"\n    📁 <code>{r['folder']}</code>" if r.get("folder") else "")
            for r in rows
        ]
        await update.message.reply_text(
            f"📝 <b>Найдено {len(rows)} MANUAL вакансий, но ни одна не готова.</b>\n\n"
            + "\n".join(lines)
            + "\n\nДобавь текст вакансии под маркером в <code>job_posting.txt</code> и повтори.",
            parse_mode=ParseMode.HTML,
        )
        return

    not_ready_count = len(rows) - len(ready)
    note = f" (ещё {not_ready_count} ожидают текста)" if not_ready_count else ""
    await update.message.reply_text(
        f"🚀 <b>Запускаю обработку {len(ready)} готовых вакансий{note}…</b>",
        parse_mode=ParseMode.HTML,
    )
    logger.info(f"[process_manual] Processing {len(ready)} ready MANUAL rows")

    ok = failed = 0
    total = len(ready)
    for i, row in enumerate(ready, 1):
        url = row["url"]
        try:
            await update.message.reply_text(
                f"⏳ [{i}/{total}] <b>{row['company']}</b> — {row['title']}\n🔗 {url}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(APPLY_AGENT_PATH),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_APPLY_AGENT_TIMEOUT)

        if proc.returncode == 0:
            ok += 1
            logger.info(f"[process_manual] OK: {url}")
        else:
            failed += 1
            logger.error(f"[process_manual] FAIL: {url}\n{stderr.decode(errors='replace')[-300:]}")

    await update.message.reply_text(
        f"🏁 <b>process_manual завершён</b>\n✅ {ok} / ❌ {failed} / Всего: {total}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import _hunt_lock
    from hunter.config import AUTO_APPLY
    from hunter.sources import ALL_SOURCES

    pending = len(_pending_jobs)
    lock_status = "🔒 Auto-apply in progress" if _hunt_lock.locked() else "🔓 Idle"
    mode = "AUTO" if AUTO_APPLY else "MANUAL"

    schedule_str = _build_schedule_text()
    await update.message.reply_text(
        f"{schedule_str}\n\n"
        f"🔧 Mode: {mode}\n"
        f"{lock_status}\n"
        f"📋 Pending decisions: {pending} jobs",
        parse_mode=ParseMode.HTML,
    )


async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the full source schedule as a clean table."""
    await update.message.reply_text(
        _build_schedule_text(),
        parse_mode=ParseMode.HTML,
    )


async def cmd_unsent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Сколько строк в очереди на отправку (как to_send.xlsx) и сколько с ANGULAR в Stack."""
    try:
        from hunter.tracker import iter_rows_for_to_send
        rows = await asyncio.to_thread(iter_rows_for_to_send)
        total = len(rows)
        angular_n = sum(
            1 for r in rows
            if "ANGULAR" in str(r.get("stack") or "").upper()
        )
        if total == 0:
            msg = "📭 <b>Неотосланных заявок нет</b> — to_send пуст."
        else:
            msg = (
                f"📋 <b>Неотосланных заявок:</b> {total}\n"
                f"🔷 <b>С ANGULAR в Stack:</b> {angular_n}"
            )
    except Exception as exc:
        logger.exception("[unsent] Failed: %s", exc)
        msg = f"❌ Не удалось прочитать трекер: <code>{exc}</code>"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def cmd_check_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Check all URLs in to_send.xlsx for expired job offers (works on a copy)."""
    from hunter.expired_to_send_check import run_check, apply_check, TO_SEND_PATH

    if not TO_SEND_PATH.exists():
        await update.message.reply_text("⚠️ to_send.xlsx не найден.")
        return

    # Count rows to check upfront
    import openpyxl as _opxl
    _wb = _opxl.load_workbook(TO_SEND_PATH)
    _ws = _wb.active
    _total_rows = max(0, _ws.max_row - 1)

    status_msg = await update.message.reply_text(
        f"🔍 Проверяю to_send.xlsx ({_total_rows} вакансий)...\n"
        f"Это займёт ~{_total_rows} секунд.\n"
        f"<i>Оригинал не трогаю — работаю с копией.</i>",
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

    if not expired:
        return

    # Try to apply immediately — will fail if Excel / LibreOffice Calc has the file open
    apply_result = await asyncio.to_thread(apply_check)
    if apply_result["ok"]:
        synced = apply_result.get("synced", 0)
        sync_note = f"\n📊 tracker.xlsx обновлён — {synced} строк(и) помечено EXPIRED." if synced else ""
        sync_err = apply_result.get("sync_error")
        sync_err_note = f"\n⚠️ sync_sent не удался: <code>{sync_err}</code>" if sync_err else ""
        await update.message.reply_text(
            f"✅ <b>to_send.xlsx обновлён</b> — истекшие вакансии помечены EXPIRED в колонке Sent."
            f"{sync_note}{sync_err_note}",
            parse_mode=ParseMode.HTML,
        )
    elif apply_result["error"] == "PermissionError":
        await update.message.reply_text(
            "📌 Файл занят (открыт в Excel или LibreOffice Calc).\n"
            "Буду пробовать каждую минуту — как закроешь, применю автоматически.",
            parse_mode=ParseMode.HTML,
        )
        _schedule_apply_retry(context)
    else:
        await update.message.reply_text(
            f"❌ Не удалось применить: <code>{apply_result['error']}</code>\n"
            f"Попробуй /apply_expired вручную.",
            parse_mode=ParseMode.HTML,
        )


_APPLY_RETRY_JOB = "apply_expired_retry"


def _schedule_apply_retry(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Schedule a repeating job that tries apply_check every 60s until it succeeds."""
    # Cancel any existing retry to avoid duplicates
    for job in context.job_queue.get_jobs_by_name(_APPLY_RETRY_JOB):
        job.schedule_removal()
    context.job_queue.run_repeating(
        _retry_apply_expired,
        interval=60,
        first=60,
        name=_APPLY_RETRY_JOB,
    )
    logger.info("[apply_expired] retry job scheduled (every 60s)")


async def _retry_apply_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Repeating job: try to apply checked copy until the editor releases the file."""
    from hunter.expired_to_send_check import apply_check, CHECKING_PATH

    if not CHECKING_PATH.exists():
        # Nothing left to apply — stop
        context.job.schedule_removal()
        return

    result = await asyncio.to_thread(apply_check)
    if result["ok"]:
        context.job.schedule_removal()
        logger.info("[apply_expired] retry succeeded — file applied")
        synced = result.get("synced", 0)
        sync_note = f"\n📊 tracker.xlsx обновлён — {synced} строк(и) помечено EXPIRED." if synced else ""
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"✅ <b>to_send.xlsx обновлён</b> — истекшие вакансии помечены EXPIRED в колонке Sent.{sync_note}",
            parse_mode=ParseMode.HTML,
        )
    else:
        logger.debug("[apply_expired] retry: file still locked, will try again in 60s")


async def cmd_apply_expired(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Replace to_send.xlsx with the checked copy (to_send_checking.xlsx)."""
    from hunter.expired_to_send_check import apply_check, CHECKING_PATH

    if not CHECKING_PATH.exists():
        await update.message.reply_text(
            "ℹ️ Нет готовых результатов проверки.\n"
            "Сначала запусти /check_expired.",
        )
        return

    result = await asyncio.to_thread(apply_check)

    if result["ok"]:
        # Cancel background retry if running
        for job in context.job_queue.get_jobs_by_name(_APPLY_RETRY_JOB):
            job.schedule_removal()
        synced = result.get("synced", 0)
        sync_note = f"\n📊 tracker.xlsx обновлён — {synced} строк(и) помечено EXPIRED." if synced else ""
        await update.message.reply_text(
            f"✅ <b>to_send.xlsx обновлён</b> — истекшие вакансии помечены EXPIRED в колонке Sent."
            f"{sync_note}",
            parse_mode=ParseMode.HTML,
        )
        logger.info("[apply_expired] Applied checking copy to to_send.xlsx")
    elif result["error"] == "PermissionError":
        await update.message.reply_text(
            "⚠️ <b>Файл занят — закрой to_send.xlsx в Excel или LibreOffice Calc.</b>\n"
            "Буду пробовать каждую минуту автоматически.",
            parse_mode=ParseMode.HTML,
        )
        _schedule_apply_retry(context)
    else:
        await update.message.reply_text(
            f"❌ Ошибка: <code>{result['error']}</code>",
            parse_mode=ParseMode.HTML,
        )


async def cmd_sync_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync Sent marks from to_send.xlsx back to tracker.xlsx, then rebuild to_send.xlsx."""
    await update.message.reply_text("⏳ Syncing to_send.xlsx → tracker.xlsx…")
    try:
        from hunter import to_send
        if to_send.is_to_send_locked_by_editor():
            await update.message.reply_text(
                "⚠️ <b>to_send.xlsx открыт в Excel или LibreOffice Calc!</b>\n\n"
                "Несохранённые изменения не будут прочитаны.\n"
                "<b>Сохрани файл (Ctrl+S) и запусти /sync_sent снова.</b>",
                parse_mode=ParseMode.HTML,
            )
            return
        result = to_send.sync_and_rebuild()
        synced = result["synced"]
        rebuilt = result["rebuilt"]
        if synced:
            msg = f"✅ Synced <b>{synced}</b> Sent mark(s) to tracker.xlsx."
        else:
            msg = "ℹ️ No new Sent marks found in to_send.xlsx."
        if rebuilt:
            msg += "\n📄 to_send.xlsx rebuilt (only unsent rows remain)."
        else:
            msg += "\n⚠️ to_send.xlsx could not be rebuilt — close the file and retry."
    except Exception as exc:
        logger.exception("[sync_sent] Failed: %s", exc)
        msg = f"❌ Sync failed: {exc}"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


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


# ── Callback handler (Apply / Skip buttons) ───────────────────────────────────

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if ":" not in data:
        return

    action, job_id = data.split(":", 1)
    job: Optional[Job] = _pending_jobs.get(job_id)

    if not job:
        await query.edit_message_text(
            query.message.text + "\n\n⚠️ Expired — restart bot and run /hunt again.",
            parse_mode=ParseMode.HTML,
        )
        return

    if action == "skip":
        await _handle_skip(query, job, job_id)
    elif action == "apply":
        await _handle_apply(query, job, job_id, context)


async def _handle_skip(query, job: Job, job_id: str) -> None:
    # Write to tracker synchronously in thread pool
    await asyncio.to_thread(add_skipped, job)
    _pending_jobs.pop(job_id, None)

    original = query.message.text
    await query.edit_message_text(
        original + "\n\n❌ <i>Skipped</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )
    logger.info(f"[Skip] {job.company} — {job.title}")


async def _handle_apply(query, job: Job, job_id: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    _pending_jobs.pop(job_id, None)

    original = query.message.text
    await query.edit_message_text(
        original + "\n\n⏳ <i>Generating documents...</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=None,
    )

    logger.info(f"[Apply] Launching apply_agent for: {job.url}")

    # Run apply_agent.py as a detached subprocess so bot stays responsive
    # apply_agent.py will send its own Telegram notification when done
    asyncio.create_task(_run_apply_agent(job.url))


_APPLY_AGENT_TIMEOUT = 900  # 15 min hard cap per job


async def _tg_notify(text: str) -> None:
    """Send a message to the configured chat via bot token (no context needed)."""
    from telegram import Bot
    try:
        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
    except Exception as e:
        logger.error(f"[tg_notify] failed: {e}")


async def _run_apply_agent(
    url: str,
    force: bool = False,
    paste_file: Optional[str] = None,
) -> None:
    """Run apply_agent.py via apply_service, don't block the event loop.

    If ``paste_file`` is set, URL may be empty — apply_agent will use the pasted
    text instead of fetching.
    """
    from hunter.services.apply_service import run_apply_agent_for_url

    label = url or "(pasted text)"
    try:
        outcome = await run_apply_agent_for_url(
            url=url,
            timeout_sec=_APPLY_AGENT_TIMEOUT,
            apply_agent_path=APPLY_AGENT_PATH,
            python_executable=sys.executable,
            force=force,
            paste_file=paste_file,
        )
        if outcome == "fail":
            logger.error(f"[apply_agent] failed for {label}")
            await _tg_notify(
                f"❌ <b>apply_agent завершился с ошибкой</b>\n🔗 {label}"
            )
        else:
            logger.info(f"[apply_agent] done ({outcome}) for {label}")
    except Exception as e:
        logger.error(f"[apply_agent] exception: {e}")
        await _tg_notify(f"❌ <b>apply_agent exception</b>\n{e}\n🔗 {label}")
    finally:
        if paste_file:
            try:
                Path(paste_file).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(
                    f"[apply_agent] could not delete paste file {paste_file}: {cleanup_err}"
                )


# ── URL message handler ───────────────────────────────────────────────────────

# Any message longer than this counts as "pasted job posting" if it isn't a single URL.
# Typical job postings are 1-4 KB; short greetings / single URLs stay well below this.
# 200 catches compact JD summaries users paste from recruiters (~250 chars) without
# reacting to casual chat.
_PASTE_TEXT_MIN_LEN = 200

_URL_RE = re.compile(r"https?://\S+")


def _looks_like_paste(text: str) -> bool:
    """True when user likely pasted a job posting (with or without URL)."""
    stripped = text.strip()
    if len(stripped) < _PASTE_TEXT_MIN_LEN:
        return False
    # Text with a URL + lots of extra content → paste with URL hint
    urls = _URL_RE.findall(stripped)
    if urls:
        non_url_len = len(_URL_RE.sub("", stripped).strip())
        return non_url_len >= _PASTE_TEXT_MIN_LEN
    # No URL at all but long message → pure paste
    return True


def _extract_url(text: str) -> str:
    """Return the first http(s) URL found in text, or ''."""
    m = _URL_RE.search(text)
    return m.group(0).rstrip(").,;") if m else ""


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

    import asyncio
    from hunter.about_me_agent import generate_about_me

    result = await asyncio.to_thread(generate_about_me, folder_path, lang)
    if not result:
        await update.message.reply_text("❌ Generation failed - check logs.")
        return

    await update.message.reply_text(result)
    await update.message.reply_text(
        f"✅ Saved to {folder_str}/About_Me_{lang.upper()}.txt"
    )


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages.

    - Long pasted job text (>= _PASTE_TEXT_MIN_LEN, with or without URL) → paste flow
    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    text = (update.message.text or "").strip()

    # Paste-mode branch: user forwarded/pasted the posting text itself.
    if _looks_like_paste(text):
        n = len(text.strip())
        await update.message.reply_text(
            f"📥 <b>Текст вакансии получен</b> — {n} симв. Сохраняю и проверяю трекер…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _handle_paste(update, text)
        return

    if not text.startswith("http"):
        await update.message.reply_text(
            "ℹ️ Отправь ссылку на вакансию (начинается с http) и я сгенерирую документы.\n"
            "Либо вставь сюда полный текст вакансии (можно с ссылкой или без) — "
            "я обработаю его напрямую.\n\n"
            "Также можно отправить ссылку из LinkedIn алерта — вытащу все вакансии из неё.",
            parse_mode=ParseMode.HTML,
        )
        return

    from job_fetch.linkedin_parse import is_linkedin_search, parse_linkedin_job_ids, job_view_url
    from hunter.config import MAX_JOBS_PER_RUN

    # Normalize LinkedIn view URLs — strip tracking params (?trk=...&refId=...)
    from job_fetch.linkedin_parse import normalize_linkedin_url
    text = normalize_linkedin_url(text)

    if is_linkedin_search(text):
        job_ids = parse_linkedin_job_ids(text)
        if not job_ids:
            await update.message.reply_text(
                "⚠️ LinkedIn ссылка распознана, но id вакансий не найдены.\n"
                "Попробуй прислать прямую ссылку на конкретную вакансию.",
                parse_mode=ParseMode.HTML,
            )
            return

        capped = job_ids[:MAX_JOBS_PER_RUN]
        skipped = len(job_ids) - len(capped)

        msg = (
            f"🔗 <b>LinkedIn алерт</b>: найдено <b>{len(job_ids)}</b> вакансий\n"
            + (f"⚠️ Обрабатываю первые {MAX_JOBS_PER_RUN} (MAX_JOBS_PER_RUN)\n" if skipped else "")
            + f"Запускаю последовательно..."
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        logger.info(f"[URL handler] LinkedIn batch: {len(capped)} jobs from alert")

        asyncio.create_task(_run_linkedin_batch(capped, update))
        return

    # Single job URL — check tracker first
    entries = await asyncio.to_thread(lookup_url, text)
    if entries:
        only_manual = all(str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries)
        if only_manual:
            from job_fetch.jobleads import try_load_manual_job_posting
            manual_content = await asyncio.to_thread(try_load_manual_job_posting, text)
            if manual_content:
                await update.message.reply_text(
                    f"✅ <b>Текст вакансии найден в файле — запускаю генерацию…</b>\n"
                    f"🔗 {text}\n\nЭто займёт 1-2 минуты.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                logger.info(f"[URL handler] MANUAL row with ready file, launching apply_agent: {text}")
                asyncio.create_task(_run_apply_agent(text))
                return
            else:
                e = entries[-1]
                folder_info = f'\n📁 <code>{e["folder"]}</code>' if e.get("folder") else ""
                await update.message.reply_text(
                    f"📝 <b>Вакансия ожидает текста (MANUAL)</b>\n\n"
                    f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}{folder_info}\n\n"
                    f"Вставь полный текст вакансии под маркером в <code>job_posting.txt</code> и пришли эту ссылку ещё раз.\n"
                    f"Либо отправь сюда текст вакансии (можно вместе со ссылкой) — обработаю сразу.",
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return

        lines = []
        for e in entries:
            sent_info = f' | Sent: {e["sent"]}' if e["sent"] else ""
            folder_info = f'\n    Folder: <code>{e["folder"]}</code>' if e["folder"] else ""
            lines.append(
                f'  Row {e["row"]}: <b>{e["company"]}</b> - {e["title"]}\n'
                f'    ATS: {e["ats"]}{sent_info}{folder_info}'
            )
        detail = "\n".join(lines)
        await update.message.reply_text(
            f"⚠️ <b>Эта вакансия уже в трекере!</b>\n\n"
            f"{detail}\n\n"
            f"Отправь /force {text}\nесли хочешь обработать заново.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        f"⏳ <b>Запускаю генерацию...</b>\n🔗 {text}\n\nЭто займёт 1-2 минуты.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[URL handler] Launching apply_agent for: {text}")
    asyncio.create_task(_run_apply_agent(text))


async def _handle_paste(update: Update, text: str, force: bool = False) -> None:
    """Save the pasted job text to a temp file and run apply_agent in paste mode.

    The URL (if found inside the text) is passed to apply_agent so it ends up in
    the tracker. If no URL — apply_agent runs without one and writes an empty URL cell.

    ``force=True`` passes ``--force`` (bypass tracker duplicate block and React-only skip).
    """
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER

    url = _extract_url(text)
    url_inferred = False

    # If URL is already tracked, only block when it is NOT a MANUAL-pending row.
    manual_pending = False
    entries = []
    if url:
        entries = await asyncio.to_thread(lookup_url, url)
        manual_pending = any(str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries)
        if entries and not manual_pending and not force:
            detail = "\n".join(
                f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}\n"
                f"    ATS: {e['ats']}"
                + (f" | Sent: {e['sent']}" if e['sent'] else "")
                for e in entries
            )
            await update.message.reply_text(
                f"⚠️ <b>Эта вакансия уже в трекере!</b>\n\n"
                f"{detail}\n\n"
                f"Отправь <code>/force {url}</code> или <code>/force</code> с полным текстом.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            return

    # If this is a MANUAL-pending JobLeads row, write into its job_posting.txt and rerun apply.
    if manual_pending and url and "jobleads.com" in url.lower():
        jp = await asyncio.to_thread(manual_jobleads_job_posting_path, url)
        if jp and jp.is_file():
            try:
                existing = jp.read_text(encoding="utf-8", errors="replace")
                if JOBLEADS_PASTE_MARKER in existing:
                    prefix, _ = existing.split(JOBLEADS_PASTE_MARKER, 1)
                    jp.write_text(prefix + JOBLEADS_PASTE_MARKER + "\n\n" + text.strip() + "\n", encoding="utf-8")
                else:
                    # Fallback: overwrite file if marker is missing for some reason.
                    jp.write_text(text.strip() + "\n", encoding="utf-8")
            except Exception as e:
                await update.message.reply_text(
                    f"❌ Не удалось записать текст в <code>{jp}</code>\n<pre>{str(e)[:500]}</pre>",
                    parse_mode=ParseMode.HTML,
                )
                return

            inferred_note = " (URL восстановил из трекера)" if url_inferred else ""
            force_note = " <code>--force</code>" if force else ""
            await update.message.reply_text(
                "✅ <b>Подтверждаю:</b> текст записан в <code>job_posting.txt</code>, "
                f"запускаю генерацию документов{force_note}.\n"
                f"🔗 {url}{inferred_note}\n\n"
                "Ориентировочно 1–2 мин; готовые файлы пришлю отдельным сообщением.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(f"[paste handler] Updated MANUAL job_posting.txt and rerun apply url={url} force={force}")
            asyncio.create_task(_run_apply_agent(url, force=force))
            return

    try:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".txt",
            prefix="tg_paste_",
            delete=False,
        )
        with tmp as fh:
            fh.write(text)
        paste_path = tmp.name
    except Exception as e:
        logger.exception("[paste handler] failed to write temp file")
        await update.message.reply_text(
            f"❌ Не удалось сохранить присланный текст во временный файл: <code>{e}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    chars = len(text)
    if url:
        inferred_note = " (URL восстановил из трекера)" if url_inferred else ""
        url_line = f"🔗 {url}{inferred_note}"
    else:
        url_line = "🔗 (ссылка не найдена — обрабатываю без неё)"
    mode = "режим вставки + <code>--force</code>" if force else "режим вставки"
    await update.message.reply_text(
        "✅ <b>Подтверждаю:</b> текст сохранён, запускаю <code>apply_agent</code> "
        f"({mode}, {chars} симв.).\n"
        f"{url_line}\n\n"
        "Ориентировочно 1–2 мин; результат пришлю сюда же.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(
        f"[paste handler] Launching apply_agent paste mode ({chars} chars) url={url or '—'} force={force}"
    )
    asyncio.create_task(_run_apply_agent(url, force=force, paste_file=paste_path))


async def _run_linkedin_batch(job_ids: list[str], update) -> None:
    """Run apply_agent sequentially for each LinkedIn job id."""
    from job_fetch.linkedin_parse import job_view_url

    total = len(job_ids)
    ok = failed = 0

    for i, jid in enumerate(job_ids, 1):
        url = job_view_url(jid)
        try:
            await update.message.reply_text(
                f"⏳ [{i}/{total}] LinkedIn job {jid}",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
        except Exception:
            pass

        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(APPLY_AGENT_PATH),
            url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode == 0:
            ok += 1
            logger.info(f"[linkedin_batch] OK job {jid}")
        else:
            failed += 1
            logger.error(f"[linkedin_batch] FAIL job {jid}: {stderr.decode(errors='replace')[-300:]}")

    try:
        await update.message.reply_text(
            f"🏁 <b>LinkedIn batch done</b>\n✅ {ok} / ❌ {failed} / Total: {total}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


# ── Application factory ───────────────────────────────────────────────────────

async def _set_bot_commands(app: Application) -> None:
    """Register command list with Telegram so the '/' menu is populated."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",          "Show help"),
        BotCommand("hunt",           "Run search (optional: source names)"),
        BotCommand("status",         "Bot status and schedule"),
        BotCommand("schedule",       "Show source schedule"),
        BotCommand("force",          "Process URL even if already in tracker"),
        BotCommand("process_manual", "Process MANUAL rows with filled job_posting.txt"),
        BotCommand("sync_sent",      "Sync Sent column from to_send.xlsx"),
        BotCommand("unsent",         "Unsent queue count + Angular in Stack"),
        BotCommand("check_expired",  "Check to_send for expired job offers"),
        BotCommand("apply_expired",  "Apply expiry results to to_send.xlsx"),
        BotCommand("about_me",       "Generate About Me for a job URL (lang + url)"),
    ])


def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    import pytz
    from datetime import time as dt_time

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_set_bot_commands).build()

    # Command handlers
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("hunt",      cmd_hunt))
    app.add_handler(CommandHandler("force",          cmd_force))
    app.add_handler(CommandHandler("process_manual", cmd_process_manual))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("schedule",  cmd_schedule))
    app.add_handler(CommandHandler("unsent",        cmd_unsent))
    app.add_handler(CommandHandler("sync_sent",     cmd_sync_sent))
    app.add_handler(CommandHandler("check_expired", cmd_check_expired))
    app.add_handler(CommandHandler("apply_expired", cmd_apply_expired))
    app.add_handler(CommandHandler("about_me",      cmd_about_me))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Plain URL messages → auto-apply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_url))

    # Staggered per-source scheduled hunts.
    # Each source gets its own daily job at: base_time + source_index * offset_min.
    # Times wrap past midnight with modulo 24h.
    tz = pytz.timezone(TIMEZONE)
    from hunter.sources import ALL_SOURCES

    for idx, source in enumerate(ALL_SOURCES):
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            fire_hour, fire_min = total // 60, total % 60

            app.job_queue.run_daily(
                callback=_scheduled_hunt,
                time=dt_time(fire_hour, fire_min, tzinfo=tz),
                name=f"hunt_{source.name}_{base_time}",
                data={"source_names": [source.name]},
            )
            logger.info(
                f"[Schedule] {source.name} at "
                f"{fire_hour:02d}:{fire_min:02d} {TIMEZONE}"
            )

    # Dedicated to_send sync — fires once per base schedule window (5 min before first hunt)
    for base_time in SCHEDULE_TIMES:
        h, m = map(int, base_time.split(":"))
        total = h * 60 + m - 5
        total %= 24 * 60
        fire_hour, fire_min = total // 60, total % 60
        app.job_queue.run_daily(
            callback=_scheduled_sync_sent,
            time=dt_time(fire_hour, fire_min, tzinfo=tz),
            name=f"sync_sent_{base_time}",
        )
        logger.info(f"[Schedule] sync_sent at {fire_hour:02d}:{fire_min:02d} {TIMEZONE}")

    # Daily pending-report at 09:00 and 21:00
    for report_hour in (9, 21):
        app.job_queue.run_daily(
            callback=_scheduled_pending_report,
            time=dt_time(report_hour, 0, tzinfo=tz),
            name=f"pending_report_{report_hour:02d}00",
        )
        logger.info(f"[Schedule] pending_report at {report_hour:02d}:00 {TIMEZONE}")

    # Daily expired check at EXPIRED_CHECK_TIME (default 00:00)
    try:
        ech, ecm = map(int, EXPIRED_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        ech, ecm = 0, 0
        logger.warning("[Schedule] Invalid EXPIRED_CHECK_TIME=%r — using 00:00", EXPIRED_CHECK_TIME)
    app.job_queue.run_daily(
        callback=_scheduled_check_expired,
        time=dt_time(ech, ecm, tzinfo=tz),
        name="check_expired_daily",
    )
    logger.info(f"[Schedule] check_expired at {ech:02d}:{ecm:02d} {TIMEZONE}")

    if TRACKER_BACKUP_ENABLED:
        try:
            bh, bm = map(int, TRACKER_BACKUP_TIME.strip().split(":"))
            bh %= 24
            bm %= 60
        except (ValueError, AttributeError):
            bh, bm = 6, 5
            logger.warning(
                "[Schedule] Invalid TRACKER_BACKUP_TIME=%r — using 06:05",
                TRACKER_BACKUP_TIME,
            )
        app.job_queue.run_daily(
            callback=_scheduled_tracker_backup,
            time=dt_time(bh, bm, tzinfo=tz),
            name="tracker_backup_daily",
        )
        logger.info(f"[Schedule] tracker_backup at {bh:02d}:{bm:02d} {TIMEZONE}")

    return app


async def _scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, applies results, syncs tracker."""
    from hunter.expired_to_send_check import run_check, apply_check, TO_SEND_PATH

    if not TO_SEND_PATH.exists():
        return

    logger.info("[scheduled_check_expired] Starting daily expired check")

    try:
        result = await run_check()
    except Exception as e:
        logger.exception("[scheduled_check_expired] run_check failed: %s", e)
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"⚠️ <b>Scheduled check_expired failed</b>\n<pre>{str(e)[:300]}</pre>",
            parse_mode=ParseMode.HTML,
        )
        return

    expired = result["expired"]
    skipped = result.get("skipped", [])
    errors  = result["errors"]

    if not expired:
        logger.info("[scheduled_check_expired] Nothing expired.")
        return

    # Try to apply
    apply_result = await asyncio.to_thread(apply_check)
    if apply_result["ok"]:
        synced = apply_result.get("synced", 0)
        sync_note = f"\n📊 tracker.xlsx: {synced} строк(и) помечено EXPIRED." if synced else ""
        lines = [f"🌙 <b>Ночная проверка истёкших</b>\n"]
        lines.append(f"⏭ Истекло: <b>{len(expired)}</b>")
        for item in expired:
            lines.append(f"  • {item['company']} — {item['title']}")
        if skipped:
            lines.append(f"⏩ Пропущено (jobleads): {len(skipped)}")
        if errors:
            lines.append(f"⚠️ Ошибок: {len(errors)}")
        lines.append(f"\n✅ to_send.xlsx обновлён.{sync_note}")
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    elif apply_result["error"] == "PermissionError":
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=f"🌙 <b>Ночная проверка:</b> найдено <b>{len(expired)}</b> истёкших.\n"
                 f"📌 to_send.xlsx открыт — закрой Excel или LibreOffice Calc и нажми /apply_expired.",
            parse_mode=ParseMode.HTML,
        )
        _schedule_apply_retry(context)
    else:
        logger.error("[scheduled_check_expired] apply_check failed: %s", apply_result["error"])


async def _scheduled_tracker_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily snapshot of tracker.xlsx + to_send.xlsx (silent on success)."""
    try:
        import asyncio as _asyncio
        from hunter.tracker_backup import run_tracker_backup

        result = await _asyncio.to_thread(run_tracker_backup)
        if not result.get("ok") or result.get("errors"):
            err = "; ".join(result.get("errors") or [])[:400]
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{err}</pre>",
                parse_mode=ParseMode.HTML,
            )
    except Exception as exc:
        logger.exception("[tracker_backup] scheduled job failed: %s", exc)
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Tracker backup failed</b>\n<pre>{str(exc)[:400]}</pre>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _scheduled_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import run_hunt
    source_names = context.job.data.get("source_names") if context.job.data else None
    try:
        await run_hunt(context, source_names=source_names)
    except Exception as e:
        label = ", ".join(source_names) if source_names else "all"
        logger.exception(f"[scheduled_hunt] Unhandled error for {label}")
        extra = ""
        if "Content_Types" in str(e) or "archive" in str(e).lower():
            extra = (
                "\n\n<i>Скорее всего повреждён или не-xlsx файл tracker.xlsx / to_send.xlsx — "
                "не ошибка борда в скобках.</i>"
            )
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Hunt error</b> ({label}):\n<pre>{str(e)[:500]}</pre>{extra}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _scheduled_sync_sent(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: sync Sent marks from to_send.xlsx → tracker.xlsx (3×/day)."""
    try:
        import asyncio as _asyncio
        from hunter import to_send as _to_send
        result = await _asyncio.to_thread(_to_send.sync_and_rebuild)
        if result["synced"]:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"📬 Synced <b>{result['synced']}</b> Sent mark(s) from to_send.xlsx → tracker.xlsx",
                parse_mode=ParseMode.HTML,
            )
    except Exception:
        logger.exception("[scheduled_sync_sent] Failed")


async def _scheduled_pending_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: report how many unsent applications are in to_send.xlsx."""
    try:
        from hunter.tracker import iter_rows_for_to_send
        rows = await asyncio.to_thread(iter_rows_for_to_send)
        total = len(rows)
        if total == 0:
            msg = "📭 <b>Неотосланных заявок нет</b> — to_send.xlsx пуст."
        else:
            fail_n = sum(1 for r in rows if r["ats"] == "FAIL")
            manual_n = sum(1 for r in rows if r["ats"] == "MANUAL")
            ready_n = total - fail_n - manual_n
            parts = [f"📋 <b>Неотосланных заявок: {total}</b>"]
            if ready_n:
                parts.append(f"  ✅ Готовы к отправке: {ready_n}")
            if manual_n:
                parts.append(f"  📝 MANUAL (нужен текст): {manual_n}")
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
