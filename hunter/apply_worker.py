"""
hunter/apply_worker.py — Apply worker loop (M1, docs/HUNT_APPLY_SPLIT_PLAN.md).

Drains the PENDING queue independently of the hunt loop. With
`APPLY_QUEUE_ENABLED=true`, `hunter/main.py`'s hunt path only fetches,
filters, dedups, and writes `PENDING` rows (`tracker.add_pending`) — it no
longer runs `_auto_apply_all` inline, so `_hunt_lock` is held for seconds,
not the hours a slow CLI batch used to hold it. This loop is the other half:
claim the oldest PENDING row, run the same `apply_agent.py` subprocess the
old inline path used, resolve the outcome, deliver, sleep, repeat — forever.

Started as a background task from `telegram_bot._post_init` when
`APPLY_QUEUE_ENABLED` is true — see the module docstring note in
`telegram_bot.py` on why it is a plain `asyncio.create_task`, not
`app.create_task` (docs/improvement-2026-09/06-OPS_PLAN.md M3: graceful
shutdown). `worker_id` exists so a future N-worker rollout (M2, deferred
until M1 proves itself in production) is a config change, not a rewrite —
today only worker 0 ever runs.

Graceful shutdown (M3): `WorkerControl` is the stop signal + claim registry
the loop below checks at every iteration boundary; `shutdown_workers()` is
called once from telegram_bot's PTB `post_shutdown` hook to drive it. See
both docstrings for the two-phase (cooperative, then cancel) design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
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
# Backoff when a claimed job is already in-flight elsewhere (manual /force
# or paste). Must be long enough that we don't spin-spam Telegram for the
# whole duration of the other run; tests patch this to 0.
_DUP_BACKOFF_SEC = 60
# Default bound for shutdown_workers()'s two phases — see its docstring.
SHUTDOWN_WAIT_TIMEOUT_SEC = 20.0
SHUTDOWN_KILL_TIMEOUT_SEC = 15.0

_last_ready_alert_at: float = 0.0
_last_dup_alert_at: float = 0.0


def claimed_by_tag() -> str:
    """Identity stamp for `tracker.claim_pending` — `hostname:pid`.

    Lets a container restart tell "a row this exact process claimed" apart
    from "a row some other host's worker still legitimately holds" (a rare
    but real shape once more than one host can run this image). The startup
    release in `telegram_bot._post_init` uses it to release every row this
    HOST claimed (any pid — a restarted container gets a new pid, so a
    match by hostname is the correct rule, not pid equality) independent of
    `APPLY_CLAIM_TIMEOUT_MIN`; `reset_stale_claims` remains the sweep for
    the cross-host case.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


class WorkerControl:
    """Coordinates graceful shutdown of apply_worker_loop task(s).

    A dedicated object rather than bare module globals so tests can build
    an isolated instance instead of mutating shared process state — see
    tests/test_graceful_stop.py. The module-level `control` singleton below
    is what production code actually shares.
    """

    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        # worker_id -> the URL currently claimed (IN_PROGRESS) by that
        # worker, or absent while idle. Set the moment claim_pending() +
        # the in-flight lock both succeed; cleared the moment the claim is
        # resolved (terminal write, release, or delete). Read by
        # shutdown_workers()'s safety-net release below.
        self._claims: dict[int, str] = {}

    def request_stop(self) -> None:
        self.stop_event.set()

    def is_stopping(self) -> bool:
        return self.stop_event.is_set()

    def set_claim(self, worker_id: int, url: str) -> None:
        self._claims[worker_id] = url

    def clear_claim(self, worker_id: int) -> None:
        self._claims.pop(worker_id, None)

    def claimed_urls(self) -> dict[int, str]:
        return dict(self._claims)

    async def sleep_or_stop(self, seconds: float) -> None:
        """Sleep up to `seconds`, waking immediately once stop is requested."""
        if seconds <= 0:
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)


# Shared by every worker started in this process (today only worker 0).
control = WorkerControl()

# worker_id -> the task running apply_worker_loop, populated by
# telegram_bot._post_init right after creation so shutdown_workers() can
# cancel + await it.
_worker_tasks: dict[int, "asyncio.Task[None]"] = {}


def register_worker_task(worker_id: int, task: "asyncio.Task[None]") -> None:
    _worker_tasks[worker_id] = task


async def shutdown_workers(
    *,
    wait_timeout: float = SHUTDOWN_WAIT_TIMEOUT_SEC,
    kill_timeout: float = SHUTDOWN_KILL_TIMEOUT_SEC,
    ctrl: "WorkerControl | None" = None,
) -> None:
    """Stop every registered worker task, escalating if it won't stop itself.

    Called once from telegram_bot's PTB `post_shutdown` hook. Two-phase,
    matching the two states a worker can be in:

      1. Idle/polling — the common case, since a worker spends most of its
         life asleep between claims. Setting the stop event wakes
         `WorkerControl.sleep_or_stop` immediately; the loop's own
         top-of-iteration check then returns cleanly. `wait_timeout` bounds
         how long we wait for that.
      2. Mid `apply_agent.py` subprocess — can legitimately run for hours
         in CLI mode, so waiting it out is not an option. If the task is
         still alive after `wait_timeout` it is cancelled outright:
         `hunter.services.apply_service.run_apply_agent_subprocess` already
         kills the subprocess on `CancelledError`, and `apply_worker_loop`'s
         own `except asyncio.CancelledError` already releases the claim —
         cancellation here is a normal, safe path, not a last resort.

    A final safety-net pass releases any URL still recorded in the control's
    claim map: `tracker.release_claim` is a no-op once the row isn't
    IN_PROGRESS anymore, so this is harmless when the task above already
    cleaned up correctly — it only matters if a task died some other,
    unanticipated way without clearing its own claim.
    """
    wc = ctrl or control
    wc.request_stop()

    tasks = [t for t in _worker_tasks.values() if not t.done()]
    if tasks:
        _done, pending = await asyncio.wait(tasks, timeout=wait_timeout)
        if pending:
            logger.warning(
                "[apply_worker] %d worker task(s) still running after %.0fs — cancelling",
                len(pending),
                wait_timeout,
            )
            for t in pending:
                t.cancel()
            await asyncio.wait(pending, timeout=kill_timeout)

    for worker_id, url in wc.claimed_urls().items():
        try:
            await asyncio.to_thread(tracker.release_claim, url)
            logger.info("[apply_worker %d] shutdown safety-net released claim: %s", worker_id, url)
        except Exception:  # noqa: BLE001
            logger.exception(
                "[apply_worker %d] shutdown safety-net release failed: %s", worker_id, url
            )
        finally:
            wc.clear_claim(worker_id)


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

            # A genuine success is proof the LLM path (API and/or CLI
            # fallback) is working again — end any outage streak so a
            # LATER, unrelated outage is treated as fresh and alerts loudly
            # instead of being silently folded into one that already ended.
            await asyncio.to_thread(llm_outage.clear_pause)
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
        # the same wall. One alert per OUTAGE STREAK, not per arm — a
        # long-running outage re-probes and re-arms roughly hourly
        # (LLM_OUTAGE_PAUSE_MIN), and a Telegram message every hour for a
        # 36h outage (real incident, 2026-08-27) buried the one alert that
        # mattered. is_fresh is False on every re-arm of the same streak.
        await asyncio.to_thread(tracker.release_claim, job.url)
        until_ts, is_fresh = await asyncio.to_thread(llm_outage.arm_pause)
        if is_fresh:
            await send_text(
                context,
                f"💳 <b>LLM outage (billing/auth)</b> — [W{worker_id}] {job.company} — {job.title} "
                "returned to the queue.\n"
                f"⏸ Auto-apply paused until <b>{llm_outage.format_until(until_ts)}</b> "
                "(<code>/llm outage clear</code> to lift early).\n\n"
                "Reaching this alert at all means the CLI subscription fallback "
                "(M4b) ALSO failed to cover the API outage — normally it absorbs "
                "one silently. Check both:\n"
                "1. Anthropic balance/key — console.anthropic.com\n"
                "2. CLI token on the server — <code>docker compose exec -it "
                "job-hunter claude setup-token</code>, put it in "
                "<code>CLAUDE_CODE_OAUTH_TOKEN</code> in .env, "
                "<code>docker compose up -d</code>\n\n"
                "This is the only alert for this outage — it won't repeat every "
                "hour while it continues. <code>/llm outage</code> or "
                "<code>/status</code> shows the live state.",
            )
        else:
            logger.warning(
                "[apply_worker %d] LLM outage streak continues (paused until %s) — "
                "%s — %s, alert already sent",
                worker_id,
                llm_outage.format_until(until_ts),
                job.company,
                job.title,
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


async def apply_worker_loop(
    context, worker_id: int = 0, *, ctrl: "WorkerControl | None" = None
) -> None:
    """Infinite loop: claim -> apply -> resolve -> deliver -> sleep -> repeat.

    Runs for the lifetime of the bot process (started once from `_post_init`
    behind `APPLY_QUEUE_ENABLED`), until `ctrl` (or the module `control`
    singleton) has `request_stop()` called on it — see `shutdown_workers()`.
    Every sleep in this function goes through `WorkerControl.sleep_or_stop`
    so a stop request wakes it immediately instead of waiting out the full
    interval, and the top of the loop re-checks `is_stopping()` on every
    iteration boundary. Any unexpected exception is logged and swallowed
    after a short sleep — this task must never die silently and leave the
    queue stuck with no worker draining it.
    """
    wc = ctrl or control
    logger.info("[apply_worker %d] started", worker_id)
    consecutive_fails = 0
    global _last_ready_alert_at, _last_dup_alert_at
    while True:
        if wc.is_stopping():
            logger.info("[apply_worker %d] stop requested — exiting loop", worker_id)
            return
        claimed_url: str | None = None
        try:
            remaining = await asyncio.to_thread(llm_outage.pause_remaining)
            if remaining > 0:
                await wc.sleep_or_stop(min(remaining, POLL_INTERVAL_SEC * 4))
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
                await wc.sleep_or_stop(POLL_INTERVAL_SEC * 4)
                continue

            row = await asyncio.to_thread(tracker.claim_pending, claimed_by_tag())
            if row is None:
                await wc.sleep_or_stop(POLL_INTERVAL_SEC)
                continue

            job = tracker.job_from_pending_row(row)
            claimed_url = job.url
            wc.set_claim(worker_id, claimed_url)

            # Same in-flight guard as hunt/manual (PR #178): claim the URL
            # BEFORE the "Processing" Telegram ping. If /force or a paste
            # already holds it, put the row back and back off — a tight
            # re-claim loop would spam Processing/Skipped pairs for the
            # whole duration of the other run (Bugbot follow-up).
            from hunter.bot.state import mark_apply_done, try_mark_apply_active

            if not try_mark_apply_active(job.url):
                await asyncio.to_thread(tracker.release_claim, job.url)
                claimed_url = None
                wc.clear_claim(worker_id)
                now = time.monotonic()
                if now - _last_dup_alert_at >= _READY_ALERT_COOLDOWN_SEC:
                    _last_dup_alert_at = now
                    try:
                        await send_text(
                            context,
                            f"⏭ [W{worker_id}] Queue paused — already generating elsewhere "
                            f"(e.g. {job.company}). Will retry when free.",
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("[apply_worker %d] dup alert failed — continuing", worker_id)
                await wc.sleep_or_stop(
                    max(APPLY_DELAY_SEC, POLL_INTERVAL_SEC * 4, _DUP_BACKOFF_SEC)
                )
                continue

            # Iteration boundary right before dispatching the (possibly
            # very long, hours in CLI mode) apply subprocess — don't kick
            # off a brand-new run at the exact moment we're shutting down.
            # try_mark_apply_active() already succeeded above, so the
            # in-flight lock is held and must be released here too.
            if wc.is_stopping():
                await asyncio.to_thread(tracker.release_claim, job.url)
                claimed_url = None
                wc.clear_claim(worker_id)
                mark_apply_done(job.url)
                logger.info(
                    "[apply_worker %d] stop requested — released %s before dispatch",
                    worker_id,
                    job.url,
                )
                return

            try:
                # Everything from here until the finally runs with the
                # in-flight URL lock held. The Processing notify is cosmetic:
                # it must neither skip the job nor escape this block — before
                # 2026-08-11 it lived ABOVE the try, so one Telegram read
                # timeout leaked the lock forever and the FIFO queue wedged
                # behind the same claimed-and-released row ("Queue paused"
                # every cooldown, no generation running).
                permalink = (job.raw or {}).get("permalink")
                text = (
                    f"⚙️ [W{worker_id}] Processing: <b>{job.company}</b> — {job.title}\n"
                    f"📍 {job.location}\n🔗 {job.url}"
                )
                if permalink:
                    text += f"\n🔗 Post: {permalink}"
                try:
                    await send_text(context, text)
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "[apply_worker %d] Processing notify failed — continuing", worker_id
                    )

                outcome = await run_apply_agent_subprocess(
                    job=job,
                    timeout_sec=APPLY_AGENT_TIMEOUT_SEC,
                    apply_agent_path=APPLY_AGENT_PATH,
                    python_executable=sys.executable,
                )

                is_fail = await _resolve_outcome(context, worker_id, job, outcome)
                claimed_url = None  # resolved (terminal / released / deleted)
                wc.clear_claim(worker_id)
                consecutive_fails = consecutive_fails + 1 if is_fail else 0

                if consecutive_fails >= _CONSECUTIVE_FAIL_LIMIT:
                    await send_text(
                        context,
                        f"🛑 [W{worker_id}] {_CONSECUTIVE_FAIL_LIMIT} consecutive failures — "
                        f"pausing {_BACKOFF_SEC // 60} min.",
                    )
                    consecutive_fails = 0
                    await wc.sleep_or_stop(_BACKOFF_SEC)
                elif APPLY_DELAY_SEC > 0:
                    await wc.sleep_or_stop(APPLY_DELAY_SEC)
            finally:
                mark_apply_done(job.url)
        except asyncio.CancelledError:
            # Cancellation is the escalation path shutdown_workers() uses
            # when the loop is stuck mid apply_agent.py subprocess (can run
            # for hours in CLI mode) — run_apply_agent_subprocess already
            # killed the subprocess by the time this runs (its own
            # except-CancelledError does that before re-raising).
            if claimed_url:
                try:
                    await asyncio.to_thread(tracker.release_claim, claimed_url)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[apply_worker %d] failed to release claim on cancel", worker_id
                    )
                finally:
                    wc.clear_claim(worker_id)
            logger.info("[apply_worker %d] cancelled, stopping", worker_id)
            raise
        except Exception:
            logger.exception("[apply_worker %d] unexpected error — continuing", worker_id)
            # Don't leave IN_PROGRESS stranded until the stale-claim sweep
            # (default 60 min) — release_claim is a no-op if resolve already
            # wrote a terminal row or deleted the placeholder.
            if claimed_url:
                try:
                    await asyncio.to_thread(tracker.release_claim, claimed_url)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[apply_worker %d] failed to release claim after error", worker_id
                    )
                finally:
                    wc.clear_claim(worker_id)
            await wc.sleep_or_stop(POLL_INTERVAL_SEC)
