"""
hunter/best_effort.py — alert on silent degradation of best-effort subsystems.

The codebase has ~290 ``except Exception`` blocks implementing a deliberate
contract: Sheets mirror, Drive upload, delivery, outreach, dual-shadow, and
the cost/verdict writers must NEVER break an apply. That contract is
correct, but it has a cost — degradation accumulates silently. Real incident
2026-07-13: a stale in-memory Drive token made every upload fail for hours;
the owner found out only because files stopped appearing on Drive ("файлы не
появляются на диске. и вообще нет").

`hunter.source_health` and `hunter.oauth_alert` already solve this
point-wise for scrapers and Google OAuth. This module generalizes the same
shape — count CONSECUTIVE failures, alert once at a threshold, recover
loudly — into one reusable primitive:

    from hunter.best_effort import best_effort

    with best_effort("gdrive.upload_application_folder"):
        ...  # existing best-effort code, unchanged

The existing try/except inside the block is NOT removed — `best_effort()`
wraps around it. A block that already swallows its own exception (returns
None/False on error) should re-raise from its except clause so the failure
still reaches `best_effort()` for counting; the swallow still happens here.

Counters live in SQLite (`subsystem_health`, see hunter.db), not in memory:
the apply pipeline runs as a subprocess per vacancy, so failures must sum
across process boundaries the same way hunter.source_health's counters do.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Callable, Generator

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TRACKER_DB_PATH
from hunter.db import ensure_subsystem_health_table, get_db

log = logging.getLogger(__name__)

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.source_health.DB_PATH / hunter.oauth_alert's cooldown pattern).
DB_PATH = TRACKER_DB_PATH

# Alert at most once per subsystem within this window — a bad run every 30
# min (e.g. the Drive backfill) would otherwise spam the chat once per cycle.
ALERT_COOLDOWN_SEC = 6 * 3600

NotifyFn = Callable[[str], None]


def _default_notify(text: str) -> None:
    """Direct, dependency-light Telegram send (mirrors hunter.oauth_alert).

    Deliberately does NOT use hunter.bot.notifications._tg_notify: that
    helper is async and this module is called from both the async bot
    process and the sync apply subprocess. A plain sync POST works from
    either without an event loop.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        import requests

        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:  # noqa: BLE001 — notifying must never raise
        log.warning("best_effort: Telegram send failed: %s", e)


def _get_row(conn: sqlite3.Connection, subsystem: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT consecutive_failures, last_alert_at FROM subsystem_health WHERE subsystem = ?",
        (subsystem,),
    ).fetchone()


def _cooldown_elapsed(last_alert_at: str | None, now_iso: str) -> bool:
    if not last_alert_at:
        return True
    try:
        last = datetime.fromisoformat(last_alert_at)
        now = datetime.fromisoformat(now_iso)
    except ValueError:
        return True
    return (now - last).total_seconds() >= ALERT_COOLDOWN_SEC


def _record_failure(subsystem: str, error: str, threshold: int) -> tuple[int, bool]:
    """Persist one more consecutive failure. Returns (failures, should_alert)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with get_db(DB_PATH) as conn:
        ensure_subsystem_health_table(conn)
        row = _get_row(conn, subsystem)
        prev_failures = row["consecutive_failures"] if row else 0
        prev_alert_at = row["last_alert_at"] if row else None
        failures = prev_failures + 1
        should_alert = failures >= threshold and _cooldown_elapsed(prev_alert_at, now)
        new_alert_at = now if should_alert else prev_alert_at
        conn.execute(
            """
            INSERT INTO subsystem_health (subsystem, consecutive_failures, last_error, last_alert_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(subsystem) DO UPDATE SET
                consecutive_failures = excluded.consecutive_failures,
                last_error = excluded.last_error,
                last_alert_at = excluded.last_alert_at
            """,
            (subsystem, failures, str(error)[:300], new_alert_at),
        )
    return failures, should_alert


def _record_success(subsystem: str) -> bool:
    """Reset the counter. Returns True if a recovery alert should fire (i.e. an
    alert had previously been sent for this subsystem)."""
    with get_db(DB_PATH) as conn:
        ensure_subsystem_health_table(conn)
        row = _get_row(conn, subsystem)
        had_alert = bool(row and row["last_alert_at"])
        conn.execute(
            """
            INSERT INTO subsystem_health (subsystem, consecutive_failures, last_error, last_alert_at)
            VALUES (?, 0, '', NULL)
            ON CONFLICT(subsystem) DO UPDATE SET
                consecutive_failures = 0,
                last_error = '',
                last_alert_at = NULL
            """,
            (subsystem,),
        )
    return had_alert


@contextmanager
def best_effort(
    subsystem: str,
    *,
    threshold: int = 3,
    notify: NotifyFn | None = None,
) -> Generator[None, None, None]:
    """Swallow any exception raised inside the block — the existing
    best-effort contract (Sheets/Drive/Telegram/shadow must never break an
    apply) — while counting CONSECUTIVE failures for `subsystem` in SQLite.

    At `threshold` consecutive failures, fires one Telegram alert (cooldown
    `ALERT_COOLDOWN_SEC` per subsystem). A success resets the counter; if an
    alert had previously fired, one recovery message follows.

    `notify` overrides the default Telegram sender — tests pass a list-
    collecting stub instead of hitting the network.
    """
    notify_fn = notify or _default_notify

    def _notify_safe(text: str) -> None:
        try:
            notify_fn(text)
        except Exception as e:  # noqa: BLE001 — notifying must never break the caller
            log.warning("best_effort(%s): notify failed: %s", subsystem, e)

    try:
        yield
    except Exception as e:  # noqa: BLE001 — best-effort contract: swallow + count
        log.warning("best_effort(%s): %s", subsystem, e)
        try:
            failures, should_alert = _record_failure(subsystem, str(e), threshold)
        except Exception as inner:  # noqa: BLE001 — telemetry must never break the caller
            log.warning("best_effort(%s): failed to record failure: %s", subsystem, inner)
            return
        if should_alert:
            _notify_safe(
                f"⚠️ <b>{subsystem}</b>: {failures} подряд сбоев, последний: {str(e)[:200]}"
            )
            log.error("best_effort(%s): alert sent (%d consecutive failures)", subsystem, failures)
    else:
        try:
            had_alert = _record_success(subsystem)
        except Exception as inner:  # noqa: BLE001
            log.warning("best_effort(%s): failed to record success: %s", subsystem, inner)
            return
        if had_alert:
            _notify_safe(f"✅ <b>{subsystem}</b> восстановился")
            log.info("best_effort(%s): recovered", subsystem)
