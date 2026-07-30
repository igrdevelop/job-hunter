"""schedules/apply_queue.py — periodic stale-claim sweep for the PENDING queue.

M1 (docs/HUNT_APPLY_SPLIT_PLAN.md). A row claimed by apply_worker_loop
(ats_status -> IN_PROGRESS, claimed_at stamped) but never resolved within
APPLY_CLAIM_TIMEOUT_MIN minutes means the worker that claimed it crashed or
was killed mid-run — this sweep resets it back to PENDING so it isn't stuck
forever. No-op when APPLY_QUEUE_ENABLED is off.
"""

import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def scheduled_reset_stale_claims(context: ContextTypes.DEFAULT_TYPE) -> None:
    import asyncio

    from hunter.config import APPLY_CLAIM_TIMEOUT_MIN, APPLY_QUEUE_ENABLED

    if not APPLY_QUEUE_ENABLED:
        return
    try:
        from hunter import tracker

        reset = await asyncio.to_thread(tracker.reset_stale_claims, APPLY_CLAIM_TIMEOUT_MIN)
        if reset:
            logger.warning(
                "[scheduled_reset_stale_claims] reset %d stale IN_PROGRESS row(s) back to PENDING",
                reset,
            )
    except Exception as e:
        logger.warning("[scheduled_reset_stale_claims] failed: %s", e)
