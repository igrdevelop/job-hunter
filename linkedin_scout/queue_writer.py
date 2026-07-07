"""Writes found candidates to a queue file the bot drains on its own hunt
cycle, instead of sending a plain Telegram notification directly (owner
decision 2026-07-08): "this is just another job source" — a candidate found
by the standalone scout should go through the SAME pipeline as every other
source (central filters, tracker dedup, a Telegram Apply/Skip card), not a
separate ad-hoc notification the owner has to manually re-paste.

See hunter/sources/linkedin_scout_relay.py (the bot-side consumer) for the
other half of this handoff.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.seen_store import SeenStore, dedup_key

logger = logging.getLogger("linkedin_scout.queue_writer")


def enqueue_candidates(
    candidates: list[ScoutCandidate], seen_store: SeenStore, queue_path: Path
) -> int:
    """Append not-yet-seen candidates to `queue_path` (atomic write).

    Dedup check happens BEFORE enqueueing (same contract as the old direct-
    Telegram notify_candidates) so the same post is never queued twice across
    scout runs. `seen_store` is marked + saved only once, after every
    candidate in this batch has been considered. Returns the count enqueued.
    """
    new_records = []
    for candidate in candidates:
        key = dedup_key(candidate.author, candidate.body)
        if seen_store.is_seen(key):
            logger.info("[linkedin_scout] skip (already seen): %s", candidate.author)
            continue
        new_records.append(
            {
                "keyword": candidate.keyword,
                "author": candidate.author,
                "body": candidate.body,
                "scouted_at": candidate.scouted_at,
                "author_profile_url": candidate.author_profile_url,
            }
        )
        seen_store.mark_seen(key)

    if not new_records:
        return 0

    existing: list[dict] = []
    if queue_path.exists():
        try:
            existing = json.loads(queue_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            existing = []
    existing.extend(new_records)

    queue_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = queue_path.with_suffix(queue_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, queue_path)

    seen_store.save()
    logger.info("[linkedin_scout] enqueued %d new candidate(s) for the bot", len(new_records))
    return len(new_records)
