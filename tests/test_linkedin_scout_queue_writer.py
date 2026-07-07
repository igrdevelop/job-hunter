"""Tests for linkedin_scout.queue_writer — the scout's handoff to the bot."""

from __future__ import annotations

import json

from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.queue_writer import enqueue_candidates
from linkedin_scout.seen_store import SeenStore, dedup_key


def _candidate(**overrides) -> ScoutCandidate:
    defaults = dict(
        keyword="angular hiring",
        author="Deloitte Poland",
        body="We're hiring an Angular Developer. Fully remote.",
        scouted_at="2026-07-08T12:00:00+00:00",
    )
    defaults.update(overrides)
    return ScoutCandidate(**defaults)


def test_enqueue_writes_new_candidate(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    candidate = _candidate()

    count = enqueue_candidates([candidate], seen_store, queue_path)

    assert count == 1
    records = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["author"] == "Deloitte Poland"
    assert records[0]["keyword"] == "angular hiring"
    assert "hiring" in records[0]["body"].lower()


def test_enqueue_marks_seen_after_writing(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    candidate = _candidate()

    enqueue_candidates([candidate], seen_store, queue_path)

    key = dedup_key(candidate.author, candidate.body)
    reloaded = SeenStore(tmp_path / "seen.json")
    assert reloaded.is_seen(key) is True


def test_enqueue_skips_already_seen(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    candidate = _candidate()
    seen_store.mark_seen(dedup_key(candidate.author, candidate.body))
    seen_store.save()

    count = enqueue_candidates([candidate], seen_store, queue_path)

    assert count == 0
    assert not queue_path.exists()


def test_enqueue_appends_to_existing_queue(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text(json.dumps([{"author": "Old One", "body": "x"}]), encoding="utf-8")

    enqueue_candidates([_candidate(author="New One")], seen_store, queue_path)

    records = json.loads(queue_path.read_text(encoding="utf-8"))
    authors = {r["author"] for r in records}
    assert authors == {"Old One", "New One"}


def test_enqueue_corrupt_existing_queue_starts_fresh(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    queue_path.write_text("{ not valid json", encoding="utf-8")

    count = enqueue_candidates([_candidate()], seen_store, queue_path)

    assert count == 1
    records = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(records) == 1


def test_enqueue_mixed_batch_only_new_ones_counted(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"
    already_seen = _candidate(author="Old Author")
    seen_store.mark_seen(dedup_key(already_seen.author, already_seen.body))
    seen_store.save()

    count = enqueue_candidates(
        [already_seen, _candidate(author="New Author")], seen_store, queue_path
    )

    assert count == 1
    records = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["author"] == "New Author"


def test_enqueue_no_leftover_tmp_file(tmp_path):
    seen_store = SeenStore(tmp_path / "seen.json")
    queue_path = tmp_path / "queue.json"

    enqueue_candidates([_candidate()], seen_store, queue_path)

    assert not (tmp_path / "queue.json.tmp").exists()
