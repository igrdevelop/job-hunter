"""Tests for hunter/sources/linkedin_scout_relay.py — the bot-side consumer
of the standalone linkedin_scout script's candidate queue.
"""

from __future__ import annotations

import json

import pytest

from hunter.sources.linkedin_scout_relay import (
    URL_PREFIX,
    LinkedInScoutRelaySource,
    append_to_queue,
)


@pytest.fixture
def relay(tmp_path, monkeypatch):
    import hunter.sources.linkedin_scout_relay as mod

    queue_path = tmp_path / "pending_candidates.json"
    monkeypatch.setattr(mod, "QUEUE_PATH", queue_path)
    return mod.LinkedInScoutRelaySource(), queue_path


def test_not_manual_only():
    """Owner decision 2026-07-08: goes through normal AUTO_APPLY handling,
    relying on the doomed-vacancy gate + central filters (not a human review
    card) to catch a bad heuristic match — see module docstring."""
    assert LinkedInScoutRelaySource.manual_only is False


def test_search_returns_empty_when_queue_file_missing(relay):
    source, _queue_path = relay
    assert source.search() == []


def test_search_returns_empty_for_empty_queue(relay):
    source, queue_path = relay
    queue_path.write_text("[]", encoding="utf-8")
    assert source.search() == []


def test_search_converts_records_to_jobs(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps(
            [
                {
                    "keyword": "angular hiring",
                    "author": "Deloitte Poland",
                    "body": "We're hiring an Angular Developer. Fully remote.",
                    "scouted_at": "2026-07-08T12:00:00+00:00",
                    "author_profile_url": "https://www.linkedin.com/in/someone",
                }
            ]
        ),
        encoding="utf-8",
    )

    jobs = source.search()

    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Deloitte Poland"
    assert job.source == "linkedin_scout_relay"
    assert job.url.startswith(URL_PREFIX)
    assert job.raw["post_text"].startswith("We're hiring")
    assert job.raw["keyword"] == "angular hiring"
    assert job.raw["author_profile_url"] == "https://www.linkedin.com/in/someone"


def test_search_drains_queue_after_reading(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps([{"author": "A", "body": "We're hiring an Angular Developer."}]),
        encoding="utf-8",
    )

    first = source.search()
    assert len(first) == 1

    second = source.search()
    assert second == []
    assert json.loads(queue_path.read_text(encoding="utf-8")) == []


def test_search_handles_corrupt_queue_file(relay):
    source, queue_path = relay
    queue_path.write_text("{ not valid json", encoding="utf-8")
    assert source.search() == []


def test_two_different_posts_get_different_urls(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps(
            [
                {"author": "A", "body": "We're hiring an Angular Developer, post one."},
                {"author": "B", "body": "We're hiring an Angular Developer, post two."},
            ]
        ),
        encoding="utf-8",
    )

    jobs = source.search()
    urls = {j.url for j in jobs}
    assert len(urls) == 2


def test_matches_url_only_the_synthetic_prefix():
    source = LinkedInScoutRelaySource()
    assert source.matches_url(f"{URL_PREFIX}abc123") is True
    assert source.matches_url("https://www.linkedin.com/jobs/view/12345/") is False


def test_fetch_text_always_raises():
    source = LinkedInScoutRelaySource()
    with pytest.raises(RuntimeError):
        source.fetch_text(f"{URL_PREFIX}abc123")


def test_missing_author_falls_back_to_unknown(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps([{"body": "We're hiring an Angular Developer."}]), encoding="utf-8"
    )
    jobs = source.search()
    assert jobs[0].company == "Unknown"


# --- append_to_queue (the /scoutfound command handler's write path) ----------


def test_append_to_queue_creates_file(relay):
    _source, queue_path = relay
    append_to_queue({"author": "Jane", "body": "We're hiring an Angular Developer."})

    records = json.loads(queue_path.read_text(encoding="utf-8"))
    assert len(records) == 1
    assert records[0]["author"] == "Jane"


def test_append_to_queue_appends_to_existing(relay):
    source, queue_path = relay
    queue_path.write_text(json.dumps([{"author": "Old", "body": "x"}]), encoding="utf-8")

    append_to_queue({"author": "New", "body": "We're hiring an Angular Developer."})

    records = json.loads(queue_path.read_text(encoding="utf-8"))
    authors = {r["author"] for r in records}
    assert authors == {"Old", "New"}


def test_append_then_search_drains_the_appended_record(relay):
    source, _queue_path = relay
    append_to_queue({"author": "Jane", "body": "We're hiring an Angular Developer."})

    jobs = source.search()

    assert len(jobs) == 1
    assert jobs[0].company == "Jane"


def test_append_to_queue_no_leftover_tmp_file(relay):
    _source, queue_path = relay
    append_to_queue({"author": "Jane", "body": "We're hiring an Angular Developer."})
    assert not (queue_path.parent / (queue_path.name + ".tmp")).exists()
