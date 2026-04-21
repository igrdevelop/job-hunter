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
)
from hunter.models import Job
from hunter.tracker import (
    add_skipped,
    latest_manual_pending,
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
        "/hunt - run search now\n"
        "/schedule - show source schedule\n"
        "/status - show schedule + bot status\n"
        "/force &lt;url&gt; - process URL even if already in tracker\n"
        "/sync_sent - sync Sent column from to_send.xlsx → tracker.xlsx\n\n"
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


async def cmd_sync_sent(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Sync Sent marks from to_send.xlsx back to tracker.xlsx, then rebuild to_send.xlsx."""
    await update.message.reply_text("⏳ Syncing to_send.xlsx → tracker.xlsx…")
    try:
        from hunter import to_send
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
    """Run apply_agent.py in the background, don't block the event loop.

    If ``paste_file`` is set, URL may be empty — apply_agent will use the pasted
    text instead of fetching.
    """
    label = url or "(pasted text)"
    cmd = [sys.executable, str(APPLY_AGENT_PATH)]
    if url:
        cmd.append(url)
    if force:
        cmd.append("--force")
    if paste_file:
        cmd.extend(["--paste-file", paste_file])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_APPLY_AGENT_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            logger.error(f"[apply_agent] hard timeout ({_APPLY_AGENT_TIMEOUT}s) for {label}")
            await _tg_notify(
                f"⏱ <b>apply_agent завис — принудительно остановлен</b>\n"
                f"Таймаут {_APPLY_AGENT_TIMEOUT // 60} мин\n🔗 {label}"
            )
            return

        if proc.returncode != 0:
            logger.error(
                f"[apply_agent] failed (rc={proc.returncode}) for {label}:\n"
                f"{stderr.decode(errors='replace')}"
            )
        else:
            logger.info(f"[apply_agent] done for {label}")

    except Exception as e:
        logger.error(f"[apply_agent] exception: {e}")
        await _tg_notify(f"❌ <b>apply_agent exception</b>\n{e}\n🔗 {label}")
    finally:
        if paste_file:
            try:
                Path(paste_file).unlink(missing_ok=True)
            except Exception as cleanup_err:
                logger.warning(f"[apply_agent] could not delete paste file {paste_file}: {cleanup_err}")


# ── URL message handler ───────────────────────────────────────────────────────

# Any message longer than this counts as "pasted job posting" if it isn't a single URL.
# Typical job postings are 1-4 KB; short greetings / single URLs are well under 300.
_PASTE_TEXT_MIN_LEN = 300

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


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages.

    - Long pasted job text (>= _PASTE_TEXT_MIN_LEN, with or without URL) → paste flow
    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    text = (update.message.text or "").strip()

    # Paste-mode branch: user forwarded/pasted the posting text itself.
    if _looks_like_paste(text):
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


async def _handle_paste(update: Update, text: str) -> None:
    """Save the pasted job text to a temp file and run apply_agent in paste mode.

    The URL (if found inside the text) is passed to apply_agent so it ends up in
    the tracker. If no URL — apply_agent runs without one and writes an empty URL cell.
    """
    from job_fetch.jobleads import JOBLEADS_PASTE_MARKER

    url = _extract_url(text)
    url_inferred = False

    # If user pasted text without URL, try to attach it to the latest MANUAL row.
    if not url:
        latest = await asyncio.to_thread(latest_manual_pending)
        if latest and latest.get("url"):
            url = latest["url"]
            url_inferred = True

    # If URL is already tracked, only block when it is NOT a MANUAL-pending row.
    manual_pending = False
    entries = []
    if url:
        entries = await asyncio.to_thread(lookup_url, url)
        manual_pending = any(str(e.get("ats") or "").strip().upper() == "MANUAL" for e in entries)
        if entries and not manual_pending:
            detail = "\n".join(
                f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}\n"
                f"    ATS: {e['ats']}"
                + (f" | Sent: {e['sent']}" if e['sent'] else "")
                for e in entries
            )
            await update.message.reply_text(
                f"⚠️ <b>Эта вакансия уже в трекере!</b>\n\n"
                f"{detail}\n\n"
                f"Отправь /force {url}\nесли хочешь обработать заново.",
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
            await update.message.reply_text(
                "✅ <b>Текст вакансии сохранён</b> — запускаю генерацию…\n"
                f"🔗 {url}{inferred_note}\n\nЭто займёт 1-2 минуты.",
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            logger.info(f"[paste handler] Updated MANUAL job_posting.txt and rerun apply url={url}")
            asyncio.create_task(_run_apply_agent(url))
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
    await update.message.reply_text(
        f"⏳ <b>Принял текст вакансии ({chars} символов), запускаю генерацию...</b>\n"
        f"{url_line}\n\nЭто займёт 1-2 минуты.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[paste handler] Launching apply_agent paste mode ({chars} chars) url={url or '—'}")
    asyncio.create_task(_run_apply_agent(url, paste_file=paste_path))


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
    app.add_handler(CommandHandler("start",     cmd_start))
    app.add_handler(CommandHandler("hunt",      cmd_hunt))
    app.add_handler(CommandHandler("force",     cmd_force))
    app.add_handler(CommandHandler("status",    cmd_status))
    app.add_handler(CommandHandler("schedule",  cmd_schedule))
    app.add_handler(CommandHandler("sync_sent", cmd_sync_sent))

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

    return app


async def _scheduled_hunt(context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.main import run_hunt
    source_names = context.job.data.get("source_names") if context.job.data else None
    try:
        await run_hunt(context, source_names=source_names)
    except Exception as e:
        label = ", ".join(source_names) if source_names else "all"
        logger.exception(f"[scheduled_hunt] Unhandled error for {label}")
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Hunt error</b> ({label}):\n<pre>{str(e)[:500]}</pre>",
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
