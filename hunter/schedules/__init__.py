"""hunter/schedules — JobQueue scheduled callbacks + registration helper."""

from __future__ import annotations

import logging
from datetime import time as dt_time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application
    import pytz as _pytz

from hunter.config import (
    SCHEDULE_TIMES,
    SCHEDULE_SOURCE_OFFSET_MIN,
    TRACKER_BACKUP_ENABLED,
    TRACKER_BACKUP_TIME,
    EXPIRED_CHECK_TIME,
    GSHEETS_ENABLED,
    GSHEETS_REFRESH_INTERVAL_MIN,
    EMAIL_RESPONSE_CHECK_TIME,
)

from hunter.schedules.hunt import scheduled_hunt
from hunter.schedules.check_expired import scheduled_check_expired
from hunter.schedules.tracker_backup import scheduled_tracker_backup
from hunter.schedules.gdrive import scheduled_gdrive_upload_missing, scheduled_gdrive_upload_logs
from hunter.schedules.gsheets import scheduled_gsheets_resync, scheduled_gsheets_pull
from hunter.schedules.pending_report import scheduled_pending_report
from hunter.schedules.email_responses import scheduled_check_email_responses
from hunter.schedules.daily_summary import scheduled_daily_summary

logger = logging.getLogger(__name__)

__all__ = [
    "register",
    "scheduled_hunt",
    "scheduled_check_expired",
    "scheduled_tracker_backup",
    "scheduled_gdrive_upload_missing",
    "scheduled_gdrive_upload_logs",
    "scheduled_gsheets_resync",
    "scheduled_gsheets_pull",
    "scheduled_pending_report",
    "scheduled_check_email_responses",
    "scheduled_daily_summary",
]


def register(app: "Application", tz: "_pytz.BaseTzInfo") -> None:
    """Wire all JobQueue callbacks into the Application.

    Called from build_application() in hunter/app.py (or telegram_bot.py).
    """
    from hunter.sources import ALL_SOURCES
    from hunter.config import TIMEZONE

    # ── Staggered per-source scheduled hunts ─────────────────────────────────
    for idx, source in enumerate(ALL_SOURCES):
        for base_time in SCHEDULE_TIMES:
            h, m = map(int, base_time.split(":"))
            total = h * 60 + m + idx * SCHEDULE_SOURCE_OFFSET_MIN
            total %= 24 * 60
            fire_hour, fire_min = total // 60, total % 60

            app.job_queue.run_daily(
                callback=scheduled_hunt,
                time=dt_time(fire_hour, fire_min, tzinfo=tz),
                name=f"hunt_{source.name}_{base_time}",
                data={"source_names": [source.name]},
            )
            logger.info(
                "[Schedule] %s at %02d:%02d %s",
                source.name, fire_hour, fire_min, TIMEZONE,
            )

    # ── Twice-daily pending report ────────────────────────────────────────────
    for report_hour in (9, 21):
        app.job_queue.run_daily(
            callback=scheduled_pending_report,
            time=dt_time(report_hour, 0, tzinfo=tz),
            name=f"pending_report_{report_hour:02d}00",
        )
        logger.info("[Schedule] pending_report at %02d:00 %s", report_hour, TIMEZONE)

    # ── Nightly expired check ─────────────────────────────────────────────────
    try:
        ech, ecm = map(int, EXPIRED_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        ech, ecm = 0, 0
        logger.warning("[Schedule] Invalid EXPIRED_CHECK_TIME=%r — using 00:00", EXPIRED_CHECK_TIME)
    app.job_queue.run_daily(
        callback=scheduled_check_expired,
        time=dt_time(ech, ecm, tzinfo=tz),
        name="check_expired_daily",
    )
    logger.info("[Schedule] check_expired at %02d:%02d %s", ech, ecm, TIMEZONE)

    # ── Daily tracker backup ──────────────────────────────────────────────────
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
            callback=scheduled_tracker_backup,
            time=dt_time(bh, bm, tzinfo=tz),
            name="tracker_backup_daily",
        )
        logger.info("[Schedule] tracker_backup at %02d:%02d %s", bh, bm, TIMEZONE)

    # ── Daily log upload to Drive at 06:10 ───────────────────────────────────
    app.job_queue.run_daily(
        callback=scheduled_gdrive_upload_logs,
        time=dt_time(6, 10, tzinfo=tz),
        name="gdrive_upload_logs_daily",
    )
    logger.info("[Schedule] gdrive_upload_logs at 06:10 %s", TIMEZONE)

    # ── Sheets resync every 5 min ─────────────────────────────────────────────
    app.job_queue.run_repeating(
        callback=scheduled_gsheets_resync,
        interval=300,
        first=60,
        name="gsheets_resync",
    )
    logger.info("[Schedule] gsheets_resync every 5 min")

    # ── Drive upload every 3 h ────────────────────────────────────────────────
    app.job_queue.run_repeating(
        callback=scheduled_gdrive_upload_missing,
        interval=3 * 3600,
        first=300,
        name="gdrive_upload_missing",
    )
    logger.info("[Schedule] gdrive_upload_missing every 3 h")

    # ── Daily email confirmation check ────────────────────────────────────────
    try:
        erch, ercm = map(int, EMAIL_RESPONSE_CHECK_TIME.strip().split(":"))
    except (ValueError, AttributeError):
        erch, ercm = 9, 0
        logger.warning(
            "[Schedule] Invalid EMAIL_RESPONSE_CHECK_TIME=%r — using 09:00",
            EMAIL_RESPONSE_CHECK_TIME,
        )
    app.job_queue.run_daily(
        callback=scheduled_check_email_responses,
        time=dt_time(erch, ercm, tzinfo=tz),
        name="check_email_responses",
    )
    logger.info("[Schedule] check_email_responses at %02d:%02d %s", erch, ercm, TIMEZONE)

    # ── Daily applications summary at 00:01 ──────────────────────────────────
    app.job_queue.run_daily(
        callback=scheduled_daily_summary,
        time=dt_time(0, 1, tzinfo=tz),
        name="daily_summary",
    )
    logger.info("[Schedule] daily_summary at 00:01 %s", TIMEZONE)

    # ── Sheets pull every GSHEETS_REFRESH_INTERVAL_MIN ───────────────────────
    if GSHEETS_ENABLED:
        pull_interval_sec = max(60, GSHEETS_REFRESH_INTERVAL_MIN * 60)
        app.job_queue.run_repeating(
            callback=scheduled_gsheets_pull,
            interval=pull_interval_sec,
            first=120,
            name="gsheets_pull",
        )
        logger.info("[Schedule] gsheets_pull every %d min", GSHEETS_REFRESH_INTERVAL_MIN)
