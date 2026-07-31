"""
hunter/apply_worker.py — Apply worker loop (M1, docs/HUNT_APPLY_SPLIT_PLAN.md).

Drains the PENDING queue independently of the hunt loop. With
`APPLY_QUEUE_ENABLED=true`, `hunter/main.py`'s hunt path only fetches,
filters, dedups, and writes `PENDING` rows (`tracker.add_pending`) — it no
longer runs `_auto_apply_all` inline, so `_hunt_lock` is held for seconds,
not the hours a slow CLI batch used to hold it. This loop is the other half:
claim the oldest PENDING row, run the same `apply_agent.py` subprocess the
old inline path used, resolve the outcome, deliver, sleep, repeat — forever.

Started as a background PTB task (`app.create_task`, tracked/error-logged by
PTB itself) from `telegram_bot._post_init` when `APPLY_QUEUE_ENABLED` is
true. `worker_id` exists so a future N-worker rollout (M2, deferred until M1
proves itself in production) is a config change, not a rewrite — today only
worker 0 ever runs.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time

from hunter import llm_outage, tracker
from hunter.config import APPLY_AGENT_PATH, APPLY_AGENT_TIMEOUT_SEC, APPLY_DELAY_SEC
from hunter.services.apply_service import run_apply_agent_subprocess
from hunter.telegram_bot import send_text

logger = logging.getLogger(__name__)

# How long to sleep when the queue is empty before polling again.
POLL_INTERVAL_SEC = 15
# Same breaker _auto_apply_all uses (a rate-limited/down host must not be
# hammered job after job) — but this loop has no finite batch to stop; it
# backs off for a while and keeps draining the queue instead of exiting.
_CONSECUTIVE_FAIL_LIMIT = 3
_BACKOFF_SEC = 300
# Don't spam Telegram every POLL_INTERVAL when apply isn't ready (no API
# key / CLI login) — one alert per this window is enough.
_READY_ALERT_COOLDOWN_SEC = 1800

_last_ready_alert_at: float = 0.0


async def _resolve_outcome(context, worker_id: int, job, outcome: str) -> bool:
    """Handle one claimed job's outcome. Returns True iff it counts as a failure
    toward the consecutive-fail breaker."""
    permalink = (job.raw or {}).get("permalink")

    if outcome == "ok":
        # apply_agent exits 0 for BOTH real success and soft aborts that write
        # no terminal tracker row (too-short text, lang/judge block, bogus
        # company — see apply_api.py sys.exit(0) paths). Soft-abort EXPIRED/
        # SKIP/REACT paths DO write a terminal row and clear the placeholder.
        # Without this check we'd deliver + claim "Done" while leaving
        # IN_PROGRESS stranded until the stale-claim sweep (Bugbot finding).
        if await asyncio.to_thread(tracker.has_successful_entry, job.url):
            from hunter.delivery import deliver_apply_now

            await deliver_apply_now(job.url)
            text = f"✅ [W{worker_id}] Done: {job.company} — {job.title}"
            if permalink:
                text += f"\n🔗 Post: {permalink}"
            await send_text(context, text)
            return False

        if await asyncio.to_thread(tracker._is_known_terminal, job.url, job.company, job.title):
            # Soft terminal (EXPIRED / SKIP / react) — apply_agent already
            # notified Telegram and cleared the placeholder. No delivery.
            return False

        # Exit 0 left only the IN_PROGRESS placeholder. Drop it so the URL
        # isn't deduped until the stale-claim sweep — matches the inline path
        # (no tracker row → vacancy returns on the next hunt).
        await asyncio.to_thread(tracker.delete_pending_row, job.url)
        await send_text(
            context,
            f"ℹ️ [W{worker_id}] Apply exited without docs: {job.company} — {job.title} "
            "— placeholder cleared, vacancy can return on the next hunt.",
        )
        return False

    if outcome == "manual":
        await send_text(
            context,
            f"📋 [W{worker_id}] <b>JobLeads — MANUAL</b>: {job.company} — {job.title}\n"
            "See message above: fill in <code>job_posting.txt</code> and Apply again with the same URL.\n"
            "<i>Tracker updated, URL dedup active.</i>",
        )
        return False

    if outcome == "llm_outage":
        # Account-level LLM failure — global state, not this vacancy's fault.
        # Put the row back to PENDING (not FAIL) so it's retried once the
        # outage clears, and arm the shared pause so the next claim attempt
        # (top of the loop) waits instead of burning another subprocess on
        # the same wall. One alert per arm, same contract as _auto_apply_all.
        await asyncio.to_thread(tracker.release_claim, job.url)
        until_ts = await asyncio.to_thread(llm_outage.arm_pause)
        await send_text(
            context,
            f"💳 <b>LLM outage (billing/auth)</b> — [W{worker_id}] {job.company} — {job.title} "
            "returned to the queue.\n"
            f"⏸ Auto-apply paused until <b>{llm_outage.format_until(until_ts)}</b> "
            "(<code>/llm outage clear</code> to lift early).\n"
            "Check the provider account/key. Vacancy was NOT marked FAIL.",
        )
        return False

    if outcome == "cli_timeout":
        # M3: infrastructure timeout, not the vacancy's fault — back to
        # PENDING (not FAIL), no fail_count escalation.
        await asyncio.to_thread(tracker.release_claim, job.url)
        await send_text(
            context,
            f"⏰ [W{worker_id}] <b>CLI timed out</b>: {job.company} — {job.title} — back in queue.",
        )
        return False

    if outcome == "rate_limited":
        # Transient 429 during fetch — leave the row as a normal FAIL like
        # _auto_apply_all does (it can be retried via the FAIL retry slots),
        # but don't let a single flaky host trip the breaker as hard as a
        # real content failure would (still counts, same as the batch loop).
        await asyncio.to_thread(tracker.add_failed, job)
        await send_text(
            context,
            f"⏳ [W{worker_id}] Rate-limited (429): {job.company} — {job.title} — will retry later.",
        )
        return True

    # "fail" (and anything unrecognized, defensively)
    await asyncio.to_thread(tracker.add_failed, job)
    await send_text(context, f"❌ [W{worker_id}] Failed: {job.company} — {job.title}")
    return True


async def apply_worker_loop(context, worker_id: int = 0) -> None:
    """Infinite loop: claim -> apply -> resolve -> deliver -> sleep -> repeat.

    Runs for the lifetime of the bot process (started once from `_post_init`
    behind `APPLY_QUEUE_ENABLED`). Any unexpected exception is logged and
    swallowed after a short sleep — this task must never die silently and
    leave the queue stuck with no worker draining it.
    """
    logger.info("[apply_worker %d] started", worker_id)
    consecutive_fails = 0
    global _last_ready_alert_at
    while True:
        try:
            remaining = await asyncio.to_thread(llm_outage.pause_remaining)
            if remaining > 0:
                await asyncio.sleep(min(remaining, POLL_INTERVAL_SEC * 4))
                continue

            # Same readiness gate _auto_apply_all used to run before the
            # batch. Without it, a missing API key / CLI login turns every
            # claimed job into a permanent FAIL row (Bugbot finding) —
            # check BEFORE claim so nothing is left IN_PROGRESS.
            from hunter.main import _check_apply_ready

            auth_error = await asyncio.to_thread(_check_apply_ready)
            if auth_error:
                now = time.monotonic()
                if now - _last_ready_alert_at >= _READY_ALERT_COOLDOWN_SEC:
                    _last_ready_alert_at = now
                    await send_text(
                        context,
                        f"🔐 [W{worker_id}] <b>Apply not ready — queue paused</b>\n"
                        f"<pre>{auth_error[:300]}</pre>\n"
                        "PENDING jobs stay queued; fix the key/CLI and they resume.",
                    )
                await asyncio.sleep(POLL_INTERVAL_SEC * 4)
                continue

            row = await asyncio.to_thread(tracker.claim_pending)
            if row is None:
                await asyncio.sleep(POLL_INTERVAL_SEC)
                continue

            job = tracker.job_from_pending_row(row)
            permalink = (job.raw or {}).get("permalink")
            text = (
                f"⚙️ [W{worker_id}] Processing: <b>{job.company}</b> — {job.title}\n"
                f"📍 {job.location}\n🔗 {job.url}"
            )
            if permalink:
                text += f"\n🔗 Post: {permalink}"
            await send_text(context, text)

            outcome = await run_apply_agent_subprocess(
                job=job,
                timeout_sec=APPLY_AGENT_TIMEOUT_SEC,
                apply_agent_path=APPLY_AGENT_PATH,
                python_executable=sys.executable,
            )

            is_fail = await _resolve_outcome(context, worker_id, job, outcome)
            consecutive_fails = consecutive_fails + 1 if is_fail else 0

            if consecutive_fails >= _CONSECUTIVE_FAIL_LIMIT:
                await send_text(
                    context,
                    f"🛑 [W{worker_id}] {_CONSECUTIVE_FAIL_LIMIT} consecutive failures — "
                    f"pausing {_BACKOFF_SEC // 60} min.",
                )
                consecutive_fails = 0
                await asyncio.sleep(_BACKOFF_SEC)
            elif APPLY_DELAY_SEC > 0:
                await asyncio.sleep(APPLY_DELAY_SEC)
        except asyncio.CancelledError:
            logger.info("[apply_worker %d] cancelled, stopping", worker_id)
            raise
        except Exception:
            logger.exception("[apply_worker %d] unexpected error — continuing", worker_id)
            await asyncio.sleep(POLL_INTERVAL_SEC)
