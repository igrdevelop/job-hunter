"""schedules/postings_prune.py — nightly TTL prune of the postings_seen table.

docs/MARKET_MEMORY_PLAN.md M1. ``hunter/postings_seen.py`` keeps one row of
listing metadata per vacancy the hunt ever saw; this job deletes rows whose
``last_seen`` is older than ``POSTINGS_TTL_DAYS`` so the table stays bounded.
No-op when ``POSTINGS_SEEN_ENABLED`` is off — a disabled writer's table is
left exactly as it is (rollback is the flag, not a migration). Wrapped in
``best_effort("postings.prune")`` so a broken table never breaks the job
queue but repeated failures still alert.
"""

import asyncio
import logging

from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def scheduled_postings_prune(context: ContextTypes.DEFAULT_TYPE) -> None:
    from hunter.config import POSTINGS_SEEN_ENABLED, POSTINGS_TTL_DAYS

    if not POSTINGS_SEEN_ENABLED:
        logger.debug("[scheduled_postings_prune] POSTINGS_SEEN_ENABLED=false — skipped")
        return

    from hunter.best_effort import best_effort
    from hunter.postings_seen import prune

    with best_effort("postings.prune"):
        deleted = await asyncio.to_thread(prune, POSTINGS_TTL_DAYS)
        logger.info(
            "[scheduled_postings_prune] deleted %d row(s) older than %d days",
            deleted,
            POSTINGS_TTL_DAYS,
        )
