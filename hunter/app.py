"""
hunter/app.py — Telegram Application factory and scheduled job callbacks.

build_application() wires together all command handlers, scheduled jobs,
and the post-init hook. Called from hunter.py (or hunter/__main__.py).
"""

import asyncio
import logging
import time as _time
from datetime import time as dt_time

import pytz
from telegram import BotCommand
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from hunter.config import (
    EXPIRED_CHECK_TIME,
    GSHEETS_ENABLED,
    GSHEETS_REFRESH_INTERVAL_MIN,
    HEALTHCHECK_PORT,
    SCHEDULE_SOURCE_OFFSET_MIN,
    SCHEDULE_TIMES,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TIMEZONE,
    TRACKER_BACKUP_ENABLED,
    TRACKER_BACKUP_TIME,
    TRACKER_PATH,
)

_start_time = _time.monotonic()
_last_hunt: dict = {"time": None, "count": 0}
from hunter.telegram_bot import (
    _load_pending,
    button_callback,
    cmd_url,
    send_job_cards,
)
from hunter.commands.status import (
    cmd_export,
    cmd_schedule,
    cmd_start,
    cmd_stats,
    cmd_status,
    cmd_unsent,
)
from hunter.commands.hunt import cmd_force, cmd_hunt, cmd_process_manual
from hunter.commands.tracker_cmds import cmd_check_expired, cmd_sync_sent
from hunter.commands.google import (
    cmd_about_me,
    cmd_gdrive_upload_missing,
    cmd_gsheets_push_missing,
    cmd_gsheets_resync,
    cmd_gsheets_status,
)

logger = logging.getLogger(__name__)


async def _post_init(app: Application) -> None:
    """Post-init hook: register bot commands, bootstrap Google Sheets, load cache."""
    await app.bot.set_my_commands([
        BotCommand("start",           "Show help"),
        BotCommand("hunt",            "Run search (optional: source names)"),
        BotCommand("status",          "Bot status and schedule"),
        BotCommand("stats",           "30-day hunt statistics"),
        BotCommand("schedule",        "Show source schedule"),
        BotCommand("force",           "Process URL even if already in tracker"),
        BotCommand("process_manual",  "Process MANUAL rows with filled job_posting.txt"),
        BotCommand("sync_sent",       "Sync Sent column from Google Sheets"),
        BotCommand("unsent",          "Unsent applications count + Angular"),
        BotCommand("export",          "Regenerate tracker.xlsx from SQLite"),
        BotCommand("check_expired",   "Check unsent rows for expired job offers"),
        BotCommand("about_me",        "Generate About Me for a job URL (lang + url)"),
        BotCommand("gsheets_status",        "Google Sheets integration status"),
        BotCommand("gsheets_resync",        "Retry dirty rows → Google Sheets"),
        BotCommand("gsheets_push_missing",  "Push tracker rows missing from Sheets"),
        BotCommand("gdrive_upload_missing", "Upload all tracker folders to Google Drive"),
    ])

    # Bootstrap / validate Google Sheets on startup.
    try:
        from hunter import gsheets_sync

        if GSHEETS_ENABLED:
            preflight = gsheets_sync.validate_startup()
            if not preflight.get("ok"):
                err = preflight.get("error", "unknown error")
                logger.error("[gsheets] startup validation failed: %s", err)
                try:
                    await app.bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"⚠️ <b>Google Sheets не готов</b>\n<code>{err}</code>",
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass
                return

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

    # Initialize SQLite store. Migrate from Excel if db is empty and Excel exists.
    try:
        from hunter import db as _db
        _db.init_db()
        if _db.is_empty():
            migrated = await asyncio.to_thread(_db.migrate_from_excel, TRACKER_PATH)
            logger.info("[startup] db: migrated %d rows from tracker.xlsx", migrated)
        else:
            logger.info("[startup] db: %d rows", _db.row_count())
    except Exception as e:
        logger.warning("[startup] db init failed: %s", e)

    # Load tracker cache from SQLite (fast, no Excel parse).
    try:
        from hunter.tracker_cache import cache
        await cache.load_from_db()
        logger.info("[startup] tracker_cache loaded")
    except Exception as e:
        logger.warning("[startup] tracker_cache load failed: %s", e)

    # Restore pending jobs so Apply/Skip buttons work after restart.
    _load_pending()

    # Start healthcheck HTTP endpoint if configured.
    if HEALTHCHECK_PORT > 0:
        try:
            await _start_healthcheck(HEALTHCHECK_PORT)
            logger.info("[healthcheck] listening on :%d/healthz", HEALTHCHECK_PORT)
        except Exception as e:
            logger.warning("[healthcheck] failed to start: %s", e)


def build_application() -> Application:
    """Build and configure the Telegram Application instance."""
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(_post_init).build()

    # ── Command handlers ──────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",          cmd_start))
    app.add_handler(CommandHandler("hunt",           cmd_hunt))
    app.add_handler(CommandHandler("force",          cmd_force))
    app.add_handler(CommandHandler("process_manual", cmd_process_manual))
    app.add_handler(CommandHandler("status",         cmd_status))
    app.add_handler(CommandHandler("stats",          cmd_stats))
    app.add_handler(CommandHandler("schedule",       cmd_schedule))
    app.add_handler(CommandHandler("unsent",         cmd_unsent))
    app.add_handler(CommandHandler("export",         cmd_export))
    app.add_handler(CommandHandler("sync_sent",      cmd_sync_sent))
    app.add_handler(CommandHandler("check_expired",  cmd_check_expired))
    app.add_handler(CommandHandler("about_me",       cmd_about_me))
    app.add_handler(CommandHandler("gsheets_status",       cmd_gsheets_status))
    app.add_handler(CommandHandler("gsheets_resync",       cmd_gsheets_resync))
    app.add_handler(CommandHandler("gsheets_push_missing", cmd_gsheets_push_missing))
    app.add_handler(CommandHandler("gdrive_upload_missing", cmd_gdrive_upload_missing))

    # Button callbacks (Apply/Skip)
    app.add_handler(CallbackQueryHandler(button_callback))

    # Plain URL messages → auto-apply
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_url))

    # ── Scheduled jobs ────────────────────────────────────────────────────────
    tz = pytz.timezone(TIMEZONE)
    from hunter.sources import ALL_SOURCES

    # Staggered per-source hunts
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
            logger.info(f"[Schedule] {source.name} at {fire_hour:02d}:{fire_min:02d} {TIMEZONE}")

    # Daily pending-report at 09:00 and 21:00
    for report_hour in (9, 21):
        app.job_queue.run_daily(
            callback=_scheduled_pending_report,
            time=dt_time(report_hour, 0, tzinfo=tz),
            name=f"pending_report_{report_hour:02d}00",
        )
        logger.info(f"[Schedule] pending_report at {report_hour:02d}:00 {TIMEZONE}")

    # Daily expired check
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

    # Daily tracker backup
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

    # Retry dirty Sheets rows every 5 minutes
    app.job_queue.run_repeating(
        callback=_scheduled_gsheets_resync,
        interval=300,
        first=60,
        name="gsheets_resync",
    )
    logger.info("[Schedule] gsheets_resync every 5 min")

    # Pull Sheets → Excel periodically
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


# ── Scheduled job callbacks ───────────────────────────────────────────────────

async def _scheduled_hunt(context) -> None:
    import time as _t
    from hunter.main import run_hunt
    source_names = context.job.data.get("source_names") if context.job.data else None
    try:
        result = await run_hunt(context, source_names=source_names)
        _last_hunt["time"] = _t.time()
        if isinstance(result, dict):
            _last_hunt["count"] = result.get("new_jobs", 0)
    except Exception as e:
        label = ", ".join(source_names) if source_names else "all"
        logger.exception(f"[scheduled_hunt] Unhandled error for {label}")
        extra = ""
        if "Content_Types" in str(e) or "archive" in str(e).lower():
            extra = (
                "\n\n<i>Скорее всего повреждён или не-xlsx файл tracker.xlsx — "
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


async def _scheduled_check_expired(context) -> None:
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

    lines = [f"🌙 <b>Ночная проверка истёкших</b>\n"]
    lines.append(f"⏭ Истекло: <b>{len(expired)}</b>")
    for item in expired:
        lines.append(f"  • {item['company']} — {item['title']}")
    if skipped:
        lines.append(f"⏩ Пропущено (jobleads): {len(skipped)}")
    if errors:
        lines.append(f"⚠️ Ошибок: {len(errors)}")
    lines.append(f"\n📊 tracker.xlsx обновлён — {len(expired)} строк(и) помечено EXPIRED.")
    await context.bot.send_message(
        chat_id=TELEGRAM_CHAT_ID,
        text="\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


async def _scheduled_tracker_backup(context) -> None:
    import asyncio as _asyncio
    from hunter.tracker_backup import run_tracker_backup

    try:
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


async def _scheduled_gsheets_resync(context) -> None:
    try:
        from hunter import gsheets_sync
        synced = await gsheets_sync.resync_dirty()
        if synced:
            logger.info("[scheduled_gsheets_resync] pushed %d dirty row(s)", synced)
    except Exception as e:
        logger.warning("[scheduled_gsheets_resync] failed: %s", e)


async def _scheduled_gsheets_pull(context) -> None:
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


async def _scheduled_pending_report(context) -> None:
    try:
        from hunter.tracker_cache import cache
        if not cache.loaded:
            await cache.load_from_excel(TRACKER_PATH)
        total = await cache.unsent_count()
        if total == 0:
            msg = "📭 <b>Неотосланных заявок нет.</b>"
        else:
            rows = await cache.all_unsent()
            fail_n = sum(1 for r in rows if r.get("ATS %") == "FAIL")
            manual_n = sum(1 for r in rows if r.get("ATS %") == "MANUAL")
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


# ── Healthcheck endpoint ──────────────────────────────────────────────────────

async def _start_healthcheck(port: int) -> None:
    """Start a lightweight aiohttp healthcheck server on the given port.

    GET /healthz → {"status": "ok", "uptime_sec": N, "last_hunt": {...}}
    Disabled when HEALTHCHECK_PORT=0 (default).
    """
    import json as _json
    from aiohttp import web  # type: ignore[import]

    async def _handler(request: web.Request) -> web.Response:
        body = _json.dumps({
            "status": "ok",
            "uptime_sec": int(_time.monotonic() - _start_time),
            "last_hunt": _last_hunt,
        })
        return web.Response(text=body, content_type="application/json")

    _app = web.Application()
    _app.router.add_get("/healthz", _handler)
    runner = web.AppRunner(_app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
