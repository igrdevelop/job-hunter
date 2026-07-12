"""
telegram_bot.py — Thin dispatcher: owns _post_init + build_application.

After the Phase 1–7 refactor, logic lives in:
  hunter/bot/         — state, keyboards, notifications, paste, formatters, apply_runner
  hunter/commands/    — one file per Telegram command handler
  hunter/schedules/   — one file per JobQueue callback; register() wires them all

Import strategy:
  - Eager: only what main.py needs at startup (send_text, send_job_cards)
    and what _post_init / build_application need directly.
  - Lazy: all command handlers + schedules — imported inside build_application()
    so tests that import hunter.telegram_bot don't pay the full 37-module cost.
  - __getattr__: backward-compat re-exports for tests that do
    `from hunter.telegram_bot import _parse_hunt_source_args` etc.
"""

import logging

from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from hunter.config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    GSHEETS_ENABLED,
)

# Always-needed by main.py and _post_init:
from hunter.bot.notifications import send_text, send_job_cards, _tg_notify
from hunter.bot.paste import _looks_like_paste, _extract_url

# Eager re-exports kept for backward compatibility (main.py + tests import these
# from here). Declared in __all__ so the linter treats them as used, not dead.
__all__ = [
    "send_text",
    "send_job_cards",
    "_tg_notify",
    "_looks_like_paste",
    "_extract_url",
    "build_application",
    "_post_init",
]

logger = logging.getLogger(__name__)

# ── Backward-compat lazy re-exports (used by tests) ──────────────────────────
# Each entry: attribute_name → (module_path, real_attribute_name)
_LAZY_ATTRS: dict[str, tuple[str, str]] = {
    # bot infrastructure
    "_pending_jobs": ("hunter.bot.state", "_pending_jobs"),
    "_active_apply_urls": ("hunter.bot.state", "_active_apply_urls"),
    "_force_waiting": ("hunter.bot.state", "_force_waiting"),
    "_APPLY_AGENT_TIMEOUT": ("hunter.bot.state", "_APPLY_AGENT_TIMEOUT"),
    "_make_keyboard": ("hunter.bot.keyboards", "_make_keyboard"),
    "_PASTE_TEXT_MIN_LEN": ("hunter.bot.paste", "_PASTE_TEXT_MIN_LEN"),
    "_URL_RE": ("hunter.bot.paste", "_URL_RE"),
    "_build_schedule_text": ("hunter.bot.formatters", "_build_schedule_text"),
    "_format_check_responses_report": ("hunter.bot.formatters", "_format_check_responses_report"),
    "_format_daily_summary": ("hunter.bot.formatters", "_format_daily_summary"),
    "_run_apply_agent": ("hunter.bot.apply_runner", "_run_apply_agent"),
    "_run_linkedin_batch": ("hunter.bot.apply_runner", "_run_linkedin_batch"),
    "_handle_paste": ("hunter.bot.apply_runner", "_handle_paste"),
    # commands
    "cmd_start": ("hunter.commands.start", "cmd_start"),
    "cmd_schedule": ("hunter.commands.schedule", "cmd_schedule"),
    "cmd_unsent": ("hunter.commands.unsent", "cmd_unsent"),
    "cmd_status": ("hunter.commands.status", "cmd_status"),
    "cmd_sync_sent": ("hunter.commands.sync_sent", "cmd_sync_sent"),
    "cmd_hunt": ("hunter.commands.hunt", "cmd_hunt"),
    "_parse_hunt_source_args": ("hunter.commands.hunt", "parse_hunt_source_args"),
    "cmd_force": ("hunter.commands.force", "cmd_force"),
    "_force_run": ("hunter.commands.force", "_force_run"),
    "_force_cleanup": ("hunter.commands.force", "_force_cleanup"),
    "cmd_process_manual": ("hunter.commands.process_manual", "cmd_process_manual"),
    "cmd_about_me": ("hunter.commands.about_me", "cmd_about_me"),
    "cmd_check_expired": ("hunter.commands.check_expired", "cmd_check_expired"),
    "cmd_debug_url": ("hunter.commands.debug_url", "cmd_debug_url"),
    "cmd_gsheets_status": ("hunter.commands.gsheets", "cmd_gsheets_status"),
    "cmd_gsheets_push_missing": ("hunter.commands.gsheets", "cmd_gsheets_push_missing"),
    "cmd_gsheets_push_sent": ("hunter.commands.gsheets", "cmd_gsheets_push_sent"),
    "cmd_gdrive_upload_missing": ("hunter.commands.gdrive", "cmd_gdrive_upload_missing"),
    "cmd_check_responses": ("hunter.commands.check_responses", "cmd_check_responses"),
    "cmd_export": ("hunter.commands.export", "cmd_export"),
    "cmd_normalize": ("hunter.commands.normalize", "cmd_normalize"),
    "cmd_funnel": ("hunter.commands.funnel", "cmd_funnel"),
    "cmd_health": ("hunter.commands.health", "cmd_health"),
    "cmd_llm": ("hunter.commands.llm", "cmd_llm"),
    "cmd_dual": ("hunter.commands.dual", "cmd_dual"),
    "cmd_url": ("hunter.commands.url_message", "cmd_url"),
    "button_callback": ("hunter.commands.url_message", "button_callback"),
    "_handle_apply": ("hunter.commands.url_message", "_handle_apply"),
    "_handle_skip": ("hunter.commands.url_message", "_handle_skip"),
    # schedules
    "_scheduled_hunt": ("hunter.schedules.hunt", "scheduled_hunt"),
    "_scheduled_check_expired": ("hunter.schedules.check_expired", "scheduled_check_expired"),
    "_scheduled_tracker_backup": ("hunter.schedules.tracker_backup", "scheduled_tracker_backup"),
    "_scheduled_gdrive_upload_missing": (
        "hunter.schedules.gdrive",
        "scheduled_gdrive_upload_missing",
    ),
    "_scheduled_gsheets_resync": ("hunter.schedules.gsheets", "scheduled_gsheets_resync"),
    "_scheduled_gsheets_pull": ("hunter.schedules.gsheets", "scheduled_gsheets_pull"),
    "_scheduled_pending_report": ("hunter.schedules.pending_report", "scheduled_pending_report"),
    "_scheduled_check_email_responses": (
        "hunter.schedules.email_responses",
        "scheduled_check_email_responses",
    ),
    "_scheduled_daily_summary": ("hunter.schedules.daily_summary", "scheduled_daily_summary"),
    "_scheduled_normalize_sent": ("hunter.schedules.normalize_sent", "scheduled_normalize_sent"),
}


def __getattr__(name: str):
    """Lazy re-export for backward compat with tests and external callers."""
    if name in _LAZY_ATTRS:
        import importlib

        mod_path, attr = _LAZY_ATTRS[name]
        mod = importlib.import_module(mod_path)
        val = getattr(mod, attr)
        # Cache in module globals so subsequent accesses don't re-import
        globals()[name] = val
        return val
    raise AttributeError(f"module 'hunter.telegram_bot' has no attribute {name!r}")


# ── Application factory ───────────────────────────────────────────────────────


async def _post_init(app: Application) -> None:
    """Post-init hook: register bot commands + validate gsheets startup."""
    from telegram import BotCommand

    await app.bot.set_my_commands(
        [
            BotCommand("start", "Show help"),
            BotCommand("hunt", "Run search (optional: source names)"),
            BotCommand("status", "Current activity: active jobs, pending, FAIL queue"),
            BotCommand("schedule", "Hunt timetable per source"),
            BotCommand("force", "Process URL even if already in tracker"),
            BotCommand("process_manual", "Process MANUAL rows with filled job_posting.txt"),
            BotCommand("sync_sent", "Sync Sent column from Google Sheets"),
            BotCommand("unsent", "Unsent applications count + Angular"),
            BotCommand("check_expired", "Check unsent rows for expired job offers"),
            BotCommand("debug_url", "Diagnose expired detection for a single URL"),
            BotCommand("about_me", "Generate About Me for a job URL (lang + url)"),
            BotCommand("gsheets_status", "Google Sheets integration status"),
            BotCommand("gsheets_push_missing", "Push tracker rows missing from Sheets"),
            BotCommand("gsheets_push_sent", "Sync Sent/EXPIRED from tracker.xlsx → Sheets"),
            BotCommand("gdrive_upload_missing", "Upload all tracker folders to Google Drive"),
            BotCommand("check_responses", "Check Gmail confirmations [days]"),
            BotCommand("export", "Export tracker as .xlsx file"),
            BotCommand("normalize", "Rebuild clean Applied Date column (L) from Sent"),
            BotCommand("funnel", "Application funnel: tracked→generated→sent→responded [days]"),
            BotCommand("health", "Per-source scraper yield report"),
            BotCommand("llm", "Show/switch active LLM profile [name]"),
            BotCommand("dual", "Toggle dual-apply A/B comparison [on|off]"),
        ]
    )

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

            async def _tg_notify_local(text: str) -> None:
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=text,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )
                except Exception as _e:
                    logger.warning("[gsheets] notify failed: %s", _e)

            result = await gsheets_sync.init_or_load_spreadsheet(notify_cb=_tg_notify_local)
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

    # Self-heal dedup state: pull Sheets → DB once at startup. This inserts rows
    # present in the shared Sheet but missing from a fresh/empty tracker.db, so
    # dedup is not "blind" after a container restart (see docs/archive/BOOTSTRAP_DEDUP_PLAN.md).
    # Runs before cache.load_from_db() so the cache sees the restored rows.
    try:
        pull_res = await gsheets_sync.pull_full_snapshot()
        logger.info(
            "[startup] gsheets pull: pulled=%s inserted=%s updated=%s reconciled=%s",
            pull_res.get("pulled"),
            pull_res.get("inserted"),
            pull_res.get("updated"),
            pull_res.get("reconciled"),
        )
    except Exception as e:
        logger.warning("[startup] gsheets pull failed: %s", e)

    # Load tracker cache so /unsent, /sync_sent, and scheduled reports are
    # correct immediately after startup (not only after the first /hunt).
    try:
        from hunter.tracker_cache import cache

        await cache.load_from_db()
        logger.info("[startup] tracker_cache loaded")
    except Exception as e:
        logger.warning("[startup] tracker_cache load failed: %s", e)


def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    import pytz

    # Import all handlers lazily — only when the bot actually starts.
    from hunter.commands.start import cmd_start
    from hunter.commands.schedule import cmd_schedule
    from hunter.commands.unsent import cmd_unsent
    from hunter.commands.status import cmd_status
    from hunter.commands.sync_sent import cmd_sync_sent
    from hunter.commands.hunt import cmd_hunt
    from hunter.commands.force import cmd_force
    from hunter.commands.process_manual import cmd_process_manual
    from hunter.commands.about_me import cmd_about_me
    from hunter.commands.check_expired import cmd_check_expired
    from hunter.commands.debug_url import cmd_debug_url
    from hunter.commands.gsheets import (
        cmd_gsheets_status,
        cmd_gsheets_push_missing,
        cmd_gsheets_push_sent,
    )
    from hunter.commands.gdrive import cmd_gdrive_upload_missing
    from hunter.commands.check_responses import cmd_check_responses
    from hunter.commands.export import cmd_export
    from hunter.commands.normalize import cmd_normalize
    from hunter.commands.funnel import cmd_funnel
    from hunter.commands.health import cmd_health
    from hunter.commands.llm import cmd_llm
    from hunter.commands.dual import cmd_dual
    from hunter.commands.scoutfound import cmd_scoutfound
    from hunter.commands.url_message import cmd_url, button_callback
    from hunter.schedules import register as _register_schedules

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("hunt", cmd_hunt))
    app.add_handler(CommandHandler("force", cmd_force))
    app.add_handler(CommandHandler("process_manual", cmd_process_manual))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("unsent", cmd_unsent))
    app.add_handler(CommandHandler("sync_sent", cmd_sync_sent))
    app.add_handler(CommandHandler("check_expired", cmd_check_expired))
    app.add_handler(CommandHandler("debug_url", cmd_debug_url))
    app.add_handler(CommandHandler("about_me", cmd_about_me))
    app.add_handler(CommandHandler("gsheets_status", cmd_gsheets_status))
    app.add_handler(CommandHandler("gsheets_push_missing", cmd_gsheets_push_missing))
    app.add_handler(CommandHandler("gsheets_push_sent", cmd_gsheets_push_sent))
    app.add_handler(CommandHandler("gdrive_upload_missing", cmd_gdrive_upload_missing))
    app.add_handler(CommandHandler("check_responses", cmd_check_responses))
    app.add_handler(CommandHandler("export", cmd_export))
    app.add_handler(CommandHandler("normalize", cmd_normalize))
    app.add_handler(CommandHandler("funnel", cmd_funnel))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("llm", cmd_llm))
    app.add_handler(CommandHandler("dual", cmd_dual))
    app.add_handler(CommandHandler("scoutfound", cmd_scoutfound))

    # Button callbacks
    app.add_handler(CallbackQueryHandler(button_callback))

    # Plain URL messages → auto-apply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_url))

    # Register all scheduled jobs via hunter.schedules
    tz = pytz.timezone(TIMEZONE)
    _register_schedules(app, tz)

    return app
