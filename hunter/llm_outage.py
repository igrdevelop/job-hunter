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

Streak suppression (2026-08-27 — a real outage sat on BOTH the paid API
*and* the CLI subscription fallback for ~36h, and the loop re-probed and
re-armed roughly hourly, each re-arm sending its own Telegram message; ~36
near-identical alerts buried the one that mattered). `arm_pause()` now
returns whether this is the FIRST arm of a continuous outage
(`is_fresh=True`) or a re-arm of one still going (`is_fresh=False`) via a
second DB key (`llm_outage_streak_since`), set on the first arm and cleared
by `clear_pause()`. Callers send the loud alert only when `is_fresh`, and
must call `clear_pause()` on the next successful apply so a later,
genuinely NEW outage is treated as fresh and alerts again.
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
_STREAK_KEY = "llm_outage_streak_since"


def arm_pause(now: float | None = None) -> tuple[int, bool]:
    """Start (or extend) the pause. Returns (until-timestamp, is_fresh).

    Re-arming while a pause is already active simply moves the deadline to
    now + LLM_OUTAGE_PAUSE_MIN — the natural behavior when the post-expiry
    probe job hits the wall again. `is_fresh` is True only the first time a
    continuous outage arms the pause; every subsequent re-arm of the SAME
    outage (streak marker already set) returns False so a caller can send
    one loud Telegram alert per outage instead of one every
    LLM_OUTAGE_PAUSE_MIN for however long the outage lasts.
    """
    from hunter.config import LLM_OUTAGE_PAUSE_MIN

    now_val = now if now is not None else time.time()
    until = int(now_val + LLM_OUTAGE_PAUSE_MIN * 60)
    _db_set(_DB_KEY, str(until))
    is_fresh = not _db_get(_STREAK_KEY)
    if is_fresh:
        _db_set(_STREAK_KEY, str(int(now_val)))
    logger.warning(
        "[llm_outage] auto-apply paused until %s%s",
        format_until(until),
        "" if is_fresh else " (streak continues — alert already sent)",
    )
    return until, is_fresh


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
    """Lift the pause and end the outage streak. True if a pause was active.

    Called both by the manual `/llm outage clear` command and automatically
    by callers on the next successful apply, so a later, genuinely NEW
    outage is treated as fresh again and alerts loudly instead of being
    silently folded into a streak that already ended.
    """
    was_active = pause_remaining() > 0
    _db_set(_DB_KEY, "0")
    _db_set(_STREAK_KEY, "")
    if was_active:
        logger.info("[llm_outage] pause cleared manually")
    return was_active


def format_until(until_ts: int) -> str:
    """Render the deadline as Warsaw wall-clock HH:MM for Telegram/logs."""
    from hunter.config import TIMEZONE

    return datetime.fromtimestamp(until_ts, tz=ZoneInfo(TIMEZONE)).strftime("%H:%M")
