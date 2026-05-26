"""
telegram_bot.py — Thin dispatcher: imports + re-exports all handlers; owns _post_init + build_application.

After the Phase 1–7 refactor, logic lives in:
  hunter/bot/         — state, keyboards, notifications, paste, formatters, apply_runner
  hunter/commands/    — one file per Telegram command handler
  hunter/schedules/   — one file per JobQueue callback; register() wires them all

This file is kept as a backward-compat shim: hunter.py and main.py import build_application,
send_text, send_job_cards from here; tests import command handlers and formatters from here.
"""

import logging

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
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    GSHEETS_ENABLED,
    TRACKER_PATH,
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

# ── Phase 6: URL handler + Apply/Skip button callbacks ───────────────────────
from hunter.commands.url_message import cmd_url, button_callback, _handle_apply, _handle_skip

# ── Phase 7: scheduled job callbacks ─────────────────────────────────────────
from hunter.schedules import (
    register as _register_schedules,
    scheduled_hunt as _scheduled_hunt,
    scheduled_check_expired as _scheduled_check_expired,
    scheduled_tracker_backup as _scheduled_tracker_backup,
    scheduled_gdrive_upload_missing as _scheduled_gdrive_upload_missing,
    scheduled_gsheets_resync as _scheduled_gsheets_resync,
    scheduled_gsheets_pull as _scheduled_gsheets_pull,
    scheduled_pending_report as _scheduled_pending_report,
    scheduled_check_email_responses as _scheduled_check_email_responses,
    scheduled_daily_summary as _scheduled_daily_summary,
)

logger = logging.getLogger(__name__)

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

    # Register all scheduled jobs via hunter.schedules
    tz = pytz.timezone(TIMEZONE)
    _register_schedules(app, tz)

    return app


