"""hunter/schedules/bot_state.py — scheduler facts into the DB for the
pipeline page (pipeline control plan, PR 1 (d)).

job-hunter-api reads tracker.db but cannot see the bot's JobQueue, so the
bot publishes what its OWN scheduler says (never a re-derivation of the
grid) into the existing `config` KV table — the same table as
`llm_outage_until`, written through the same helper
(hunter.llm_profiles._db_set). JSON values, shared contract keys:

  bot_state.next_hunt  = {"at": iso_utc, "source": name, "sources_total": n}
  bot_state.next_retry = {"at": iso_utc}
  bot_state.sources    = ["justjoin", ...]   (every registered source, for
                                              the page's per-source buttons)
  bot_state.updated_at = iso_utc             (written LAST; stale > 5 min
                                              means the page shows "bot
                                              offline")

`next_*` is `null` when no such job is scheduled (or before the JobQueue has
started — APScheduler only computes next run times once it runs). Refreshed
every 60 s and once right at startup; wrapped in `best_effort("bot.state")`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

KEY_NEXT_HUNT = "bot_state.next_hunt"
KEY_NEXT_RETRY = "bot_state.next_retry"
KEY_SOURCES = "bot_state.sources"
KEY_UPDATED_AT = "bot_state.updated_at"

# Job-name prefixes set by hunter/schedules/__init__.py::register.
_HUNT_PREFIX = "hunt_"
_RETRY_PREFIX = "retry_failed_"


def _iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _next_t(job: Any) -> datetime | None:
    """A job's next run time; None when unknown. PTB's Job.next_t raises
    AttributeError while the JobQueue has not started yet."""
    try:
        value = job.next_t
    except AttributeError:
        return None
    return value if isinstance(value, datetime) else None


def _earliest(jobs: Any, prefix: str) -> tuple[Any, datetime] | None:
    best: tuple[Any, datetime] | None = None
    for job in jobs:
        name = getattr(job, "name", None) or ""
        if not name.startswith(prefix):
            continue
        at = _next_t(job)
        if at is None:
            continue
        if best is None or at < best[1]:
            best = (job, at)
    return best


def collect_state(job_queue: Any, source_names: list[str]) -> dict[str, str]:
    """The four KV values (already JSON-encoded) from a JobQueue.

    Pure — no DB access — so tests can drive it with a fake job queue.
    `bot_state.updated_at` is always last in the returned dict.
    """
    jobs = job_queue.jobs() if job_queue is not None else ()
    hunt = _earliest(jobs, _HUNT_PREFIX)
    retry = _earliest(jobs, _RETRY_PREFIX)

    next_hunt: dict[str, Any] | None = None
    if hunt is not None:
        job, at = hunt
        data = getattr(job, "data", None)
        names = data.get("source_names") if isinstance(data, dict) else None
        source = names[0] if isinstance(names, list) and names else ""
        next_hunt = {"at": _iso_utc(at), "source": source, "sources_total": len(source_names)}
    next_retry = {"at": _iso_utc(retry[1])} if retry is not None else None

    return {
        KEY_NEXT_HUNT: json.dumps(next_hunt),
        KEY_NEXT_RETRY: json.dumps(next_retry),
        KEY_SOURCES: json.dumps(list(source_names), ensure_ascii=False),
        KEY_UPDATED_AT: json.dumps(_iso_utc(datetime.now(timezone.utc))),
    }


def write_state(values: dict[str, str]) -> None:
    """Write the KV rows in order (updated_at last)."""
    from hunter.llm_profiles import _db_set

    for key, value in values.items():
        _db_set(key, value)


async def publish(app_or_context: Any) -> None:
    """Collect from the running JobQueue and write — best-effort.

    Accepts an Application (startup) or a CallbackContext (the tick): both
    expose `.job_queue`.
    """
    from hunter.best_effort import best_effort
    from hunter.sources import ALL_SOURCES

    with best_effort("bot.state"):
        values = collect_state(
            getattr(app_or_context, "job_queue", None), [s.name for s in ALL_SOURCES]
        )
        await asyncio.to_thread(write_state, values)


async def scheduled_bot_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    """JobQueue callback, every 60 s (hunter/schedules/__init__.py)."""
    await publish(context)
