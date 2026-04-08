"""
telegram_bot.py — Telegram bot: notifications, inline buttons, callback handlers.

Pending jobs are stored in memory (dict job_id → Job) per session.
If the bot restarts, old buttons become "expired" — that's acceptable.
"""

import asyncio
import logging
import subprocess
import sys
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
)
from hunter.models import Job
from hunter.tracker import add_skipped, lookup_url, lookup_company, normalize_url

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
        "/hunt - run search now\n"
        "/status - show schedule\n"
        "/force &lt;url&gt; - process URL even if already in tracker\n\n"
        "Or just send a job URL to generate docs.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_hunt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual trigger — runs the full hunt cycle immediately."""
    await update.message.reply_text("🔍 Running hunt now...")
    from hunter.main import run_hunt
    await run_hunt(context)


async def cmd_force(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Force-process a URL even if it's already in tracker."""
    args = context.args
    if not args:
        await update.message.reply_text(
            "Usage: /force <url>",
            parse_mode=ParseMode.HTML,
        )
        return
    url = args[0].strip()
    if not url.startswith("http"):
        await update.message.reply_text("URL must start with http")
        return

    await update.message.reply_text(
        f"⏳ <b>Force: запускаю генерацию...</b>\n🔗 {url}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[Force] Launching apply_agent --force for: {url}")
    asyncio.create_task(_run_apply_agent(url, force=True))


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import _hunt_lock
    from hunter.config import AUTO_APPLY
    from hunter.sources import ALL_SOURCES

    pending = len(_pending_jobs)
    lock_status = "🔒 Auto-apply in progress" if _hunt_lock.locked() else "🔓 Idle"
    mode = "AUTO" if AUTO_APPLY else "MANUAL"

    # Build per-source schedule table
    schedule_lines = []
    for idx, source in enumerate(ALL_SOURCES):
        times = []
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            times.append(f"{total // 60:02d}:{total % 60:02d}")
        schedule_lines.append(f"  <b>{source.name}</b>: {' / '.join(times)}")

    schedule_str = "\n".join(schedule_lines)
    await update.message.reply_text(
        f"⏰ <b>Schedule</b> ({TIMEZONE}, offset={SCHEDULE_SOURCE_OFFSET_MIN}min):\n"
        f"{schedule_str}\n\n"
        f"🔧 Mode: {mode}\n"
        f"{lock_status}\n"
        f"📋 Pending decisions: {pending} jobs",
        parse_mode=ParseMode.HTML,
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


async def _run_apply_agent(url: str, force: bool = False) -> None:
    """Run apply_agent.py in the background, don't block the event loop."""
    try:
        cmd = [sys.executable, str(APPLY_AGENT_PATH), url]
        if force:
            cmd.append("--force")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            logger.error(f"[apply_agent] failed:\n{stderr.decode()}")
        else:
            logger.info(f"[apply_agent] done for {url}")
    except Exception as e:
        logger.error(f"[apply_agent] exception: {e}")


# ── URL message handler ───────────────────────────────────────────────────────

async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain URL messages.

    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    text = (update.message.text or "").strip()

    if not text.startswith("http"):
        await update.message.reply_text(
            "ℹ️ Отправь ссылку на вакансию (начинается с http) и я сгенерирую документы.\n\n"
            "Также можно отправить ссылку из LinkedIn алерта — вытащу все вакансии из неё.",
            parse_mode=ParseMode.HTML,
        )
        return

    from job_fetch.linkedin_parse import is_linkedin_search, parse_linkedin_job_ids, job_view_url
    from hunter.config import MAX_JOBS_PER_RUN

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
        lines = []
        for e in entries:
            status = "Sent" if e["sent"] else e["ats"] or "?"
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

def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    import pytz
    from datetime import time as dt_time

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("hunt",   cmd_hunt))
    app.add_handler(CommandHandler("force",  cmd_force))
    app.add_handler(CommandHandler("status", cmd_status))

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

    return app


async def _scheduled_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import run_hunt
    source_names = context.job.data.get("source_names") if context.job.data else None
    await run_hunt(context, source_names=source_names)
