"""LLM outage pause — time-boxed auto-apply stop after an account-level failure.

M1 (docs/LLM_OUTAGE_RESILIENCE_PLAN.md) stops ONE batch when the LLM account
dies (drained balance / bad key → llm_client.LLMOutageError → exit 46).
Without a pause the next staggered source slot (~40 min apart, 25 sources ×
3 base cycles/day) fetches its whole listing again just to die on the same
wall — the fetch cost, the anti-bot budget and the alert repeat all day.

The pause lives in the config KV table of tracker.db (same table/pattern as
`active_llm_profile` / `dual_apply_enabled`), NOT in a module global: the
apply pipeline runs in a SUBPROCESS, so only the DB crosses that boundary —
the same reason source_health's counters live in SQLite.

Time-boxed (LLM_OUTAGE_PAUSE_MIN, default 60), not sticky: after expiry the
next slot probes naturally with one job / one API call; if the account is
still dead, M1 fires again and re-arms the pause. A top-up therefore heals
the bot on its own. The Telegram alert is sent once when the pause is ARMED
(by the batch loop that saw the outage) — skipped slots only log, so a
60-minute pause never turns into an hour of repeated alerts.

Manual controls: `/llm outage` shows state, `/llm outage clear` lifts the
pause early; `/status` shows the pause while armed.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

# Same private KV helpers the profile system uses — third copy of the sqlite
# scaffolding would be worse than the cross-module import.
from hunter.llm_profiles import _db_get, _db_set

logger = logging.getLogger(__name__)

_DB_KEY = "llm_outage_until"


def arm_pause(now: float | None = None) -> int:
    """Start (or extend) the pause. Returns the until-timestamp (unix seconds).

    Re-arming while a pause is already active simply moves the deadline to
    now + LLM_OUTAGE_PAUSE_MIN — the natural behavior when the post-expiry
    probe job hits the wall again.
    """
    from hunter.config import LLM_OUTAGE_PAUSE_MIN

    until = int((now if now is not None else time.time()) + LLM_OUTAGE_PAUSE_MIN * 60)
    _db_set(_DB_KEY, str(until))
    logger.warning("[llm_outage] auto-apply paused until %s", format_until(until))
    return until


def pause_remaining(now: float | None = None) -> int:
    """Seconds of pause left; 0 when no pause is active (or the key is garbage)."""
    raw = _db_get(_DB_KEY)
    if not raw:
        return 0
    try:
        until = int(float(raw))
    except (TypeError, ValueError):
        return 0
    left = until - (now if now is not None else time.time())
    return max(0, int(left))


def clear_pause() -> bool:
    """Lift the pause early (/llm outage clear). True if one was active."""
    was_active = pause_remaining() > 0
    _db_set(_DB_KEY, "0")
    if was_active:
        logger.info("[llm_outage] pause cleared manually")
    return was_active


def format_until(until_ts: int) -> str:
    """Render the deadline as Warsaw wall-clock HH:MM for Telegram/logs."""
    from hunter.config import TIMEZONE

    return datetime.fromtimestamp(until_ts, tz=ZoneInfo(TIMEZONE)).strftime("%H:%M")
