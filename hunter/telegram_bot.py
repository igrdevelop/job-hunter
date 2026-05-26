"""
telegram_bot.py — Telegram bot: notifications, inline buttons, callback handlers.

Pending jobs are stored in memory (dict job_id → Job) per session.
If the bot restarts, old buttons become "expired" — that's acceptable.

Refactoring in progress (Phase 1+):
  Shared state   → hunter/bot/state.py
  Keyboards      → hunter/bot/keyboards.py
  Notifications  → hunter/bot/notifications.py
  Paste helpers  → hunter/bot/paste.py
  Formatters     → hunter/bot/formatters.py
"""

import asyncio
import logging
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
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
    GSHEETS_ENABLED,
    GSHEETS_REFRESH_INTERVAL_MIN,
    TRACKER_PATH,
    EMAIL_RESPONSE_CHECK_TIME,
    EMAIL_RESPONSE_LOOKBACK_DAYS,
)
from hunter.models import Job
from hunter.tracker import (
    add_skipped,
    lookup_url,
    lookup_company,
    manual_jobleads_job_posting_path,
    normalize_url,
)

# ── Phase 1: bot/ infrastructure ─────────────────────────────────────────────
from hunter.bot.state import (
    _pending_jobs,
    _active_apply_urls,
    _force_waiting,
    _APPLY_AGENT_TIMEOUT,
)
from hunter.bot.keyboards import _make_keyboard
from hunter.bot.notifications import send_text, send_job_cards, _tg_notify
from hunter.bot.paste import _PASTE_TEXT_MIN_LEN, _URL_RE, _looks_like_paste, _extract_url
from hunter.bot.formatters import (
    _build_schedule_text,
    _format_check_responses_report,
    _format_daily_summary,
)
from hunter.bot.apply_runner import _run_apply_agent, _run_linkedin_batch, _handle_paste

# ── Phase 3: simple command handlers ─────────────────────────────────────────
from hunter.commands.start import cmd_start
from hunter.commands.schedule import cmd_schedule
from hunter.commands.unsent import cmd_unsent
from hunter.commands.status import cmd_status
from hunter.commands.sync_sent import cmd_sync_sent

# ── Phase 4: commands with state / sub-logic ──────────────────────────────────
from hunter.commands.hunt import cmd_hunt, parse_hunt_source_args as _parse_hunt_source_args
from hunter.commands.force import cmd_force, _force_run, _force_cleanup
from hunter.commands.process_manual import cmd_process_manual
from hunter.commands.about_me import cmd_about_me

# ── Phase 5: heavy / grouped command handlers ─────────────────────────────────
from hunter.commands.check_expired import cmd_check_expired
from hunter.commands.debug_url import cmd_debug_url
from hunter.commands.gsheets import cmd_gsheets_status, cmd_gsheets_push_missing, cmd_gsheets_push_sent
from hunter.commands.gdrive import cmd_gdrive_upload_missing
from hunter.commands.check_responses import cmd_check_responses

logger = logging.getLogger(__name__)


# ── Public API (called from main.py) — imported from hunter.bot.notifications ─


# ── Command handlers ──────────────────────────────────────────────────────────
# cmd_start         → hunter.commands.start
# cmd_schedule      → hunter.commands.schedule
# cmd_unsent        → hunter.commands.unsent
# cmd_status        → hunter.commands.status
# cmd_sync_sent     → hunter.commands.sync_sent
# cmd_hunt          → hunter.commands.hunt
# cmd_force         → hunter.commands.force
# cmd_process_manual → hunter.commands.process_manual
# cmd_about_me      → hunter.commands.about_me


# cmd_check_expired → hunter.commands.check_expired


# cmd_debug_url → hunter.commands.debug_url


# cmd_sync_sent → hunter.commands.sync_sent
# cmd_gsheets_status, cmd_gsheets_push_missing, cmd_gsheets_push_sent → hunter.commands.gsheets
# cmd_gdrive_upload_missing → hunter.commands.gdrive

# ── Email response checker ───────────────────────────────────────────────────
# _format_check_responses_report → hunter.bot.formatters
# cmd_check_responses → hunter.commands.check_responses


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
    row = await asyncio.to_thread(add_skipped, job)
    _pending_jobs.pop(job_id, None)
    if row:
        try:
            from hunter.tracker_cache import cache
            await cache.add(row)
            from hunter import gsheets_sync
            await gsheets_sync.mirror_new_row(row)
        except Exception as _e:
            logger.warning("[skip] cache/gsheets update failed: %s", _e)

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


# _APPLY_AGENT_TIMEOUT, _active_apply_urls → hunter.bot.state
# _tg_notify             → hunter.bot.notifications
# _run_apply_agent       → hunter.bot.apply_runner
# _run_linkedin_batch    → hunter.bot.apply_runner
# _handle_paste          → hunter.bot.apply_runner
# _PASTE_TEXT_MIN_LEN, _URL_RE, _looks_like_paste, _extract_url → hunter.bot.paste

# ── URL message handler ───────────────────────────────────────────────────────

# cmd_about_me → hunter.commands.about_me


async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle plain text messages.

    - If chat is in _force_waiting state → treat as force URL/text input
    - Long pasted job text (>= _PASTE_TEXT_MIN_LEN, with or without URL) → paste flow
    - Single job URL (JustJoin, NoFluffJobs, LinkedIn /jobs/view/...) → apply_agent
    - LinkedIn search / alert URL (/jobs/search?...) → extract job ids → batch apply
    """
    text = (update.message.text or "").strip()
    chat_id = update.effective_chat.id

    # Force two-step: user replied after bare /force
    if chat_id in _force_waiting:
        _force_waiting.discard(chat_id)
        url = _extract_url(text) if text.startswith("http") else None
        await _force_run(update, url=url, body=text)
        return

    # Paste-mode branch: user forwarded/pasted the posting text itself.
    if _looks_like_paste(text):
        n = len(text.strip())
        await update.message.reply_text(
            f"📥 <b>Job posting received</b> — {n} chars. Saving and checking tracker…",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        await _handle_paste(update, text)
        return

    if not text.startswith("http"):
        await update.message.reply_text(
            "ℹ️ Send a job URL (starting with http) to generate docs.\n"
            "Or paste the full job posting text (with or without a URL) — "
            "it will be processed directly.\n\n"
            "You can also send a LinkedIn alert URL — all job ids will be extracted.",
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
                "⚠️ LinkedIn URL recognised but no job ids found.\n"
                "Try sending a direct link to a specific vacancy.",
                parse_mode=ParseMode.HTML,
            )
            return

        capped = job_ids[:MAX_JOBS_PER_RUN]
        skipped = len(job_ids) - len(capped)

        msg = (
            f"🔗 <b>LinkedIn alert</b>: found <b>{len(job_ids)}</b> jobs\n"
            + (f"⚠️ Processing first {MAX_JOBS_PER_RUN} (MAX_JOBS_PER_RUN)\n" if skipped else "")
            + "Starting sequentially…"
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
                    f"✅ <b>Job posting found in file — starting generation…</b>\n"
                    f"🔗 {text}\n\nEstimated 1–2 minutes.",
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
                    f"📝 <b>Vacancy waiting for text (MANUAL)</b>\n\n"
                    f"  Row {e['row']}: <b>{e['company']}</b> - {e['title']}{folder_info}\n\n"
                    f"Paste the full job text below the marker in <code>job_posting.txt</code> and send this URL again.\n"
                    f"Or send the job text here (with or without the URL) — it will be processed immediately.",
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
            f"⚠️ <b>This vacancy is already in the tracker!</b>\n\n"
            f"{detail}\n\n"
            f"Send /force {text}\nto process it again.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    await update.message.reply_text(
        f"⏳ <b>Starting generation…</b>\n🔗 {text}\n\nEstimated 1–2 minutes.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    logger.info(f"[URL handler] Launching apply_agent for: {text}")
    asyncio.create_task(_run_apply_agent(text))


# _handle_paste, _run_apply_agent, _run_linkedin_batch → hunter.bot.apply_runner

# ── Application factory ───────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    """Post-init hook: register bot commands + validate gsheets startup."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("start",           "Show help"),
        BotCommand("hunt",            "Run search (optional: source names)"),
        BotCommand("status",          "Current activity: active jobs, pending, FAIL queue"),
        BotCommand("schedule",        "Hunt timetable per source"),
        BotCommand("force",           "Process URL even if already in tracker"),
        BotCommand("process_manual",  "Process MANUAL rows with filled job_posting.txt"),
        BotCommand("sync_sent",       "Sync Sent column from Google Sheets"),
        BotCommand("unsent",          "Unsent applications count + Angular"),
        BotCommand("check_expired",   "Check unsent rows for expired job offers"),
        BotCommand("debug_url",       "Diagnose expired detection for a single URL"),
        BotCommand("about_me",        "Generate About Me for a job URL (lang + url)"),
        BotCommand("gsheets_status",        "Google Sheets integration status"),
        BotCommand("gsheets_push_missing",  "Push tracker rows missing from Sheets"),
        BotCommand("gsheets_push_sent",     "Sync Sent/EXPIRED from tracker.xlsx → Sheets"),
        BotCommand("gdrive_upload_missing", "Upload all tracker folders to Google Drive"),
        BotCommand("check_responses",       "Check Gmail confirmations [days]"),
    ])

    # Bootstrap / validate Google Sheets on startup.
    try:
        from hunter import gsheets_sync

        if GSHEETS_ENABLED:
            # Pre-flight: credentials + token check (sync, cheap)
            preflight = gsheets_sync.validate_startup()
            if not preflight.get("ok"):
                err = preflight.get("error", "unknown error")
                logger.error("[gsheets] startup validation failed: %s", err)
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ <b>Google Sheets not ready</b>\n<code>{err}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return  # don't try to bootstrap if creds are broken

            async def _tg_notify(text: str) -> None:
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as _e:
                    logger.warning("[gsheets] notify failed: %s", _e)

            result = await gsheets_sync.init_or_load_spreadsheet(notify_cb=_tg_notify)
            if result.get("error"):
                logger.error("[gsheets] init failed: %s", result["error"])
            else:
                url = result.get("sheet_url", "")
                created = result.get("created", False)
                logger.info(
                    "[gsheets] %s — %s",
                    "created new spreadsheet" if created else "loaded existing spreadsheet",
                    url,
                )
    except Exception as e:
        logger.warning("[gsheets] startup init failed: %s", e)

    # Load tracker cache so /unsent, /sync_sent, and scheduled reports are
    # correct immediately after startup (not only after the first /hunt).
    try:
        from hunter.tracker_cache import cache
        await cache.load_from_excel(TRACKER_PATH)
        logger.info("[startup] tracker_cache loaded")
    except Exception as e:
        logger.warning("[startup] tracker_cache load failed: %s", e)


def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    import pytz
    from datetime import time as dt_time

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("hunt",           cmd_hunt))
    app.add_handler(CommandHandler("force",          cmd_force))
    app.add_handler(CommandHandler("process_manual", cmd_process_manual))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("schedule",       cmd_schedule))
    app.add_handler(CommandHandler("unsent",         cmd_unsent))
    app.add_handler(CommandHandler("sync_sent",      cmd_sync_sent))
    app.add_handler(CommandHandler("check_expired",  cmd_check_expired))
    app.add_handler(CommandHandler("debug_url",      cmd_debug_url))
    app.add_handler(CommandHandler("about_me",       cmd_about_me))
    app.add_handler(CommandHandler("gsheets_status",       cmd_gsheets_status))
    app.add_handler(CommandHandler("gsheets_push_missing", cmd_gsheets_push_missing))
    app.add_handler(CommandHandler("gsheets_push_sent",    cmd_gsheets_push_sent))
    app.add_handler(CommandHandler("gdrive_upload_missing", cmd_gdrive_upload_missing))
    app.add_handler(CommandHandler("check_responses",      cmd_check_responses))

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

    # Retry dirty Sheets rows every 5 minutes (no-op when GSHEETS_ENABLED=false)
    app.job_queue.run_repeating(
        callback=_scheduled_gsheets_resync,
        interval=300,
        first=60,
        name="gsheets_resync",
    )
    logger.info("[Schedule] gsheets_resync every 5 min")

    # Upload missing Drive folders every 3 hours (no-op when GDRIVE_ENABLED=false)
    app.job_queue.run_repeating(
        callback=_scheduled_gdrive_upload_missing,
        interval=3 * 3600,
        first=300,
        name="gdrive_upload_missing",
    )
    logger.info("[Schedule] gdrive_upload_missing every 3 h")

    # Daily email confirmation check at EMAIL_RESPONSE_CHECK_TIME (default 09:00)
    try:
        erch, ercm = map(int, EMAIL_RESPONSE_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        erch, ercm = 9, 0
        logger.warning(
            "[Schedule] Invalid EMAIL_RESPONSE_CHECK_TIME=%r — using 09:00",
            EMAIL_RESPONSE_CHECK_TIME,
        )
    app.job_queue.run_daily(
        callback=_scheduled_check_email_responses,
        time=dt_time(erch, ercm, tzinfo=tz),
        name="check_email_responses",
    )
    logger.info("[Schedule] check_email_responses at %02d:%02d %s", erch, ercm, TIMEZONE)

    # Daily applications summary at 00:01 Warsaw time
    app.job_queue.run_daily(
        callback=_scheduled_daily_summary,
        time=dt_time(0, 1, tzinfo=tz),
        name="daily_summary",
    )
    logger.info("[Schedule] daily_summary at 00:01 %s", TIMEZONE)

    # Pull Sheets → Excel every GSHEETS_REFRESH_INTERVAL_MIN (no-op when disabled)
    if GSHEETS_ENABLED:
        pull_interval_sec = max(60, GSHEETS_REFRESH_INTERVAL_MIN * 60)
        app.job_queue.run_repeating(
            callback=_scheduled_gsheets_pull,
            interval=pull_interval_sec,
            first=120,
            name="gsheets_pull",
        )
        logger.info("[Schedule] gsheets_pull every %d min", GSHEETS_REFRESH_INTERVAL_MIN)

    return app


async def _scheduled_check_expired(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily scheduled expired check — runs at midnight, marks EXPIRED in tracker.xlsx."""
    from hunter.expired_marker import run_check

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

    lines = [f"🌙 <b>Nightly expired check</b>\n"]
    lines.append(f"⏭ Expired: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Skipped (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Errors: {len(errors)}")
    lines.append(f"\n📊 tracker.xlsx updated — {len(expired)} row(s) marked EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def _scheduled_tracker_backup(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily snapshot of tracker.xlsx (silent on success)."""
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
                "\n\n<i>Likely corrupt or non-xlsx tracker.xlsx file — "
                "not a board error in parentheses.</i>"
            )
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=f"⚠️ <b>Hunt error</b> ({label}):\n<pre>{str(e)[:500]}</pre>{extra}",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass


async def _scheduled_gdrive_upload_missing(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-3-hour job: upload application folders missing from Google Drive (no-op if disabled)."""
    from hunter.config import GDRIVE_ENABLED, PROJECT_DIR
    if not GDRIVE_ENABLED:
        return
    try:
        from hunter import gdrive_sync
        result = await gdrive_sync.upload_missing_folders(PROJECT_DIR)
        uploaded = result.get("uploaded", 0)
        if uploaded:
            logger.info("[scheduled_gdrive_upload_missing] uploaded %d folder(s)", uploaded)
    except Exception as e:
        logger.warning("[scheduled_gdrive_upload_missing] failed: %s", e)


async def _scheduled_gsheets_resync(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every-5-min job: push dirty rows to Google Sheets (no-op if disabled)."""
    try:
        from hunter import gsheets_sync
        synced = await gsheets_sync.resync_dirty()
        if synced:
            logger.info("[scheduled_gsheets_resync] pushed %d dirty row(s)", synced)
    except Exception as e:
        logger.warning("[scheduled_gsheets_resync] failed: %s", e)


async def _scheduled_gsheets_pull(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: pull Sheets → tracker.xlsx (every GSHEETS_REFRESH_INTERVAL_MIN)."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        from hunter import gsheets_sync
        result = await gsheets_sync.pull_full_snapshot()
        updated = result.get("updated", 0)
        if updated:
            logger.info("[scheduled_gsheets_pull] updated %d row(s) from Sheets", updated)
        if result.get("errors"):
            logger.warning("[scheduled_gsheets_pull] errors: %s", result["errors"])
    except Exception as e:
        logger.warning("[scheduled_gsheets_pull] failed: %s", e)


async def _scheduled_pending_report(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Scheduled job: report how many unsent applications are in tracker."""
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        if total == 0:
            msg = "📭 <b>No unsent applications.</b>"
        else:
            rows = await cache.all_unsent()
            fail_n = sum(1 for r in rows if r.get("ATS %") == "FAIL")
            manual_n = sum(1 for r in rows if r.get("ATS %") == "MANUAL")
            ready_n = total - fail_n - manual_n
            parts = [f"📋 <b>Unsent applications: {total}</b>"]
            if ready_n:
                parts.append(f"  ✅ Ready to send: {ready_n}")
            if manual_n:
                parts.append(f"  📝 MANUAL (text needed): {manual_n}")
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


async def _scheduled_check_email_responses(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Periodic job: check Gmail for new confirmation emails; notify only if new ones found."""
    try:
        from hunter.email_response_checker import run_confirmation_check
        results = await asyncio.to_thread(run_confirmation_check)
    except FileNotFoundError:
        logger.debug("[scheduled_check_email_responses] gmail_token.json missing — skipping")
        return
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] failed: %s", e)
        return

    # Only notify about rows that were just written (response was empty before this run)
    newly_confirmed = [
        r for r in results
        if r.match_type in ("exact", "fuzzy")
        and r.row_num is not None
        and not r.candidates[0].get("confirmation", "")
    ]
    if not newly_confirmed:
        return

    lines = [f"📬 <b>New confirmations from email ({len(newly_confirmed)}):</b>"]
    for r in newly_confirmed:
        c = r.candidates[0]
        lines.append(f"  • <b>{c['company']}</b> — {c['title']} <i>[{r.email.platform}]</i>")

    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text="\n".join(lines),
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_check_email_responses] send failed: %s", e)


# ── Daily applications summary ────────────────────────────────────────────────
# _format_daily_summary → hunter.bot.formatters

async def _scheduled_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily job at 00:01: send a summary of how many applications were made yesterday."""
    from datetime import date as _date, timedelta
    from hunter.tracker import get_applications_on_date

    yesterday = (_date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        apps = await asyncio.to_thread(get_applications_on_date, yesterday)
    except Exception as e:
        logger.warning("[scheduled_daily_summary] failed to read tracker: %s", e)
        return

    if not apps:
        return  # silent when nothing was applied to yesterday

    text = _format_daily_summary(apps, yesterday)
    try:
        await context.bot.send_message(
            chat_id=TELEGRAM_CHAT_ID,
            text=text,
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        logger.warning("[scheduled_daily_summary] send failed: %s", e)
