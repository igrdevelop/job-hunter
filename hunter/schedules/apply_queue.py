"""schedules/apply_queue.py — periodic stale-claim + orphan-run sweep.

Two independent sweeps share one 15-min tick:

1. Stale claims (M1, docs/HUNT_APPLY_SPLIT_PLAN.md). A row claimed by
   apply_worker_loop (ats_status -> IN_PROGRESS, claimed_at stamped) but never
   resolved within APPLY_CLAIM_TIMEOUT_MIN minutes means the worker that
   claimed it crashed or was killed mid-run — reset it back to PENDING so it
   isn't stuck forever. Guarded by APPLY_QUEUE_ENABLED: with the queue off
   there are no claims to sweep.
2. Orphan generation_runs (docs/PIPELINE_VIZ_PLAN.md M1, 2026-09-22). A
   `generation_runs` row whose apply subprocess died without `finish_run`
   normally gets stamped by the parent right after the subprocess exits
   (`hunter.services.apply_service._settle_orphan_run`); this sweep is the
   net under THAT — a parent that died with its subprocess, or a paste-mode
   run with no url_norm to match on — stamping `orphan:stale` on any open run
   older than APPLY_AGENT_CLI_TIMEOUT_SEC, the widest wall clock a legitimate
   run can have. NOT guarded by APPLY_QUEUE_ENABLED: the inline hunt path
   spawns the same subprocess and leaks the same way, so the callback is
   registered unconditionally and only sweep 1 checks the flag.
"""

import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def scheduled_reset_stale_claims(context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    from hunter.config import (
        APPLY_AGENT_CLI_TIMEOUT_SEC,
        APPLY_CLAIM_TIMEOUT_MIN,
        APPLY_QUEUE_ENABLED,
    )

    if APPLY_QUEUE_ENABLED:
        try:
            from hunter import tracker

            reset = await asyncio.to_thread(tracker.reset_stale_claims, APPLY_CLAIM_TIMEOUT_MIN)
            if reset:
                logger.warning(
                    "[scheduled_reset_stale_claims] reset %d stale IN_PROGRESS row(s) "
                    "back to PENDING",
                    reset,
                )
        except Exception as e:
            logger.warning("[scheduled_reset_stale_claims] failed: %s", e)

    # Orphan-run sweep — runs regardless of the queue flag (module docstring).
    # metrics.reset_stale_open_runs is already inside best_effort("metrics")
    # and returns 0 on a DB failure; the try/except here only guards the
    # import + thread hop so this tick can never take the JobQueue down.
    try:
        from hunter import metrics

        stale = await asyncio.to_thread(metrics.reset_stale_open_runs, APPLY_AGENT_CLI_TIMEOUT_SEC)
        if stale:
            logger.warning(
                "[scheduled_reset_stale_claims] stamped %d orphan generation_runs row(s) "
                "as %s (open longer than %ds)",
                stale,
                metrics.STALE_OUTCOME,
                APPLY_AGENT_CLI_TIMEOUT_SEC,
            )
    except Exception as e:
        logger.warning("[scheduled_reset_stale_claims] orphan-run sweep failed: %s", e)
