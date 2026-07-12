"""LinkedIn Scout relay — no scraping here.

The actual LinkedIn scraping happens in the standalone `linkedin_scout/`
script (separate process, owner's own Windows desktop, own Task Scheduler
cadence — see linkedin_scout/README.md). That script does NOT share a
filesystem with the bot (the bot auto-deploys to its own server/container),
so a candidate it finds is relayed over Telegram instead of a shared file:
`linkedin_scout/telegram_relay.py` sends a `/scoutfound <payload>` command
through the OWNER'S OWN Telegram user session (not the bot's — Telegram never
delivers a bot's own outgoing messages back to itself as an incoming update,
so this can't work as a plain bot-to-itself message). `hunter/commands/
scoutfound.py` receives that command and calls `append_to_queue()` below,
which writes into `pending_candidates.json` **on the bot's own filesystem**
— this module's `search()` then drains that same, now-local, file on the
bot's own hunt cycle. Both the append and the drain happen inside this one
process (guarded by `_LOCK`), so there is no cross-machine or cross-process
race the way a shared file written by two independent machines would have.

Each candidate goes through the EXACT same pipeline as every other source:
central filters, the doomed-vacancy gate, tracker dedup, and — per owner
decision 2026-07-08 ("we dropped confirmation cards long ago, I never wait
for them; there's already a full check pipeline other job-board postings go
through, I want these to go through it too") — normal AUTO_APPLY handling,
NOT `manual_only`. A HARD doomed-gate finding still aborts generation for
$0.00 exactly like any other source (paste-mode does NOT downgrade HARD
findings — only genuine `/force` does, see hunter/apply_shared.py::
run_doomed_gate's `is_force_override`), so a bad heuristic match still gets
caught downstream, just not by a human looking at a card first.

There is no real fetchable URL for a LinkedIn feed post used for dedup/routing
— `job.url` stays the synthetic `URL_PREFIX` key on purpose (a real
linkedin.com URL here would collide with `LinkedInSource.matches_url`, which
claims any URL containing "linkedin.com" regardless of path and is registered
before this source in `_fetch_roster()`). So `fetch_text()` always raises;
apply for these jobs routes through the paste flow instead — both the
AUTO_APPLY path (`hunter.services.apply_service.run_apply_agent_subprocess`)
and the manual Telegram-card path (`hunter/commands/url_message.py::
_handle_apply`, kept for when AUTO_APPLY is off) detect `job.raw["post_text"]`
and use it instead of fetching `job.url`.

Some posts DO expose a real, clickable permalink in their DOM (owner discovery
2026-07-08, live-verified — an earlier probe had found none reachable, which
turned out to be wrong for at least LinkedIn "share"-type posts). When the
scout's browser.py captures one, it's carried through as `job.raw["permalink"]`
(best-effort, `None` when absent) purely for the owner's convenience — it is
NOT used for dedup/fetch/routing, only surfaced in the pre-apply Telegram
notification (see `hunter/main.py::_auto_apply_all`) so the owner can follow
the actual post if they want to.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading

from hunter.config import PROJECT_DIR
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

QUEUE_PATH = PROJECT_DIR / "linkedin_scout" / "pending_candidates.json"

# Synthetic URL prefix — a dedup key for tracker.db, not a real navigable
# LinkedIn URL. Deliberately NOT on the linkedin.com host: an earlier version
# used "https://linkedin.com/scout-posts/p..." which LOOKED distinctive but
# still collided with LinkedInSource.matches_url() (hostname-only check,
# "linkedin.com" in host — matches any path) — and LinkedInSource is
# registered before this source in _fetch_roster(). Any fetch_job_text(url)
# call for a scout URL that ISN'T short-circuited by paste_text (a synthetic
# row picked up by /debug_url, or by expired_marker's periodic unsent-row
# check — FAIL rows aren't excluded from iter_unsent_rows) got silently
# routed to the real LinkedIn fetcher instead of raising the intended
# "no fetchable URL" error, and it tried to actually fetch a nonexistent
# linkedin.com path. The ".internal" pseudo-TLD below can never resolve to a
# real host and matches no other source's matches_url() (all check real
# domains), so no precedence handling is needed and none can ever be needed.
# The per-post hash MUST live in the path, not the fragment: normalize_url()
# strips fragments, and a fragment-based key made every scout job normalize
# to the same url_norm — dedup then dropped every candidate after the first
# (issue #144).
URL_PREFIX = "https://linkedin-scout.internal/posts/p"

# Guards QUEUE_PATH read/append/clear against the append (called from the
# async /scoutfound command handler, via asyncio.to_thread) racing the drain
# (called from the hunt loop's `asyncio.to_thread(source.search)`) — both run
# in this one process's thread pool, so a plain threading.Lock is enough;
# there is no second machine/process touching this file anymore.
_LOCK = threading.Lock()


def append_to_queue(record: dict) -> None:
    """Append one candidate record to the queue (atomic write, lock-guarded).

    Called by hunter/commands/scoutfound.py when the owner's Telegram user
    session relays a `/scoutfound` command from the standalone scout script.
    """
    with _LOCK:
        existing: list[dict] = []
        if QUEUE_PATH.exists():
            try:
                existing = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                existing = []
        existing.append(record)

        QUEUE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = QUEUE_PATH.with_suffix(QUEUE_PATH.suffix + ".tmp")
        tmp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_path, QUEUE_PATH)
    logger.info("[linkedin_scout_relay] appended 1 candidate via /scoutfound (queue now %d)", len(existing))


class LinkedInScoutRelaySource(BaseSource):
    name = "linkedin_scout_relay"
    # NOT manual_only — see module docstring. Goes through AUTO_APPLY exactly
    # like every other source; the doomed-vacancy gate + central filters are
    # what's relied on to catch a bad heuristic match, not a human review step.

    def search(self) -> list[Job]:
        with _LOCK:
            if not QUEUE_PATH.exists():
                return []
            try:
                records = json.loads(QUEUE_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("[linkedin_scout_relay] failed to read queue: %s", e)
                return []
            if not records:
                return []

            jobs: list[Job] = [self._record_to_job(rec) for rec in records]

            # Drain the queue now that it's been read — each record surfaces
            # exactly once. If a job below gets filtered/deduped, that's the
            # same outcome as any other source re-listing an already-seen job.
            try:
                QUEUE_PATH.write_text("[]", encoding="utf-8")
            except OSError as e:
                logger.warning("[linkedin_scout_relay] failed to clear queue: %s", e)

        logger.info("[linkedin_scout_relay] drained %d queued candidate(s)", len(jobs))
        return jobs

    @staticmethod
    def _record_to_job(rec: dict) -> Job:
        author = rec.get("author", "") or "Unknown"
        body = rec.get("body", "") or ""
        key = hashlib.md5(f"{author}{body[:200]}".encode("utf-8")).hexdigest()[:16]
        snippet = " ".join(body.strip().split())[:70]
        return Job(
            title=f"[LI post] {snippet}",
            company=author,
            location="",
            salary=None,
            url=f"{URL_PREFIX}{key}",
            source="linkedin_scout_relay",
            raw={
                "post_text": body,
                "keyword": rec.get("keyword", ""),
                "author_profile_url": rec.get("author_profile_url"),
                "scouted_at": rec.get("scouted_at", ""),
                "permalink": rec.get("permalink"),
            },
        )

    def matches_url(self, url: str) -> bool:
        return url.startswith(URL_PREFIX)

    def fetch_text(self, url: str) -> str:
        raise RuntimeError(
            "linkedin_scout posts have no fetchable URL — apply must use the "
            "paste flow with the saved post text (job.raw['post_text'])."
        )
