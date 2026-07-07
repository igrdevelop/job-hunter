"""LinkedIn Scout relay — no scraping here.

The actual LinkedIn scraping happens in the standalone `linkedin_scout/`
script (separate process, owner's own Windows desktop, own Task Scheduler
cadence — see linkedin_scout/README.md). That script writes candidates it
found (already filtered through its own hiring heuristic) to a small JSON
queue file. This source's job is only to drain that queue on the bot's own
hunt schedule and turn each entry into a normal `Job`, so it goes through the
EXACT same pipeline as every other source: central filters, tracker dedup,
and a Telegram Apply/Skip card — never auto-applied (`manual_only = True`),
since a regex-heuristic match deserves a human confirmation before any LLM
spend, unlike a structured job-board listing.

There is no real fetchable URL for a LinkedIn feed post (verified in the
scout's own design docs — no permalinks are reachable without extra clicking,
which was rejected as added bot surface). So `fetch_text()` always raises;
the Apply button for these jobs routes through the paste flow instead
(`hunter/commands/url_message.py::_handle_apply`), using the saved
`raw["post_text"]`.
"""

from __future__ import annotations

import hashlib
import json
import logging

from hunter.config import PROJECT_DIR
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

QUEUE_PATH = PROJECT_DIR / "linkedin_scout" / "pending_candidates.json"

# Synthetic URL prefix — a dedup key for tracker.db, not a real navigable
# LinkedIn URL. Distinctive enough to never collide with a real linkedin.com
# URL, so it needs no special precedence handling in the fetch dispatcher.
URL_PREFIX = "https://linkedin.com/scout-posts/#p"


class LinkedInScoutRelaySource(BaseSource):
    name = "linkedin_scout_relay"
    manual_only = True

    def search(self) -> list[Job]:
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
            },
        )

    def matches_url(self, url: str) -> bool:
        return url.startswith(URL_PREFIX)

    def fetch_text(self, url: str) -> str:
        raise RuntimeError(
            "linkedin_scout posts have no fetchable URL — apply must use the "
            "paste flow with the saved post text (job.raw['post_text'])."
        )
