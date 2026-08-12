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
    assert job.raw["permalink"] is None


def test_search_carries_permalink_through_when_present(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps(
            [
                {
                    "keyword": "angular hiring",
                    "author": "Deloitte Poland",
                    "body": "We're hiring an Angular Developer. Fully remote.",
                    "scouted_at": "2026-07-08T12:00:00+00:00",
                    "permalink": "https://www.linkedin.com/feed/update/urn:li:share:42/",
                }
            ]
        ),
        encoding="utf-8",
    )

    jobs = source.search()

    assert jobs[0].raw["permalink"] == "https://www.linkedin.com/feed/update/urn:li:share:42/"
    # the synthetic dedup URL must stay untouched by the real permalink
    assert jobs[0].url.startswith(URL_PREFIX)


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


# --- Payload v2: the scout's jobs track ("job" kind) -------------------------
#
# A jobs-track record is the opposite of a feed post in every way that matters:
# it has a REAL canonical LinkedIn url, so that url stays job.url (dedup against
# LinkedInSource's own finds, expired-check and FAIL retries all key on it), the
# bot fetches the description itself, and post_text must be ABSENT so apply does
# not reroute through the paste flow.

JOB_RECORD = {
    "v": 2,
    "kind": "job",
    "keyword": "angular",
    "url": "https://www.linkedin.com/jobs/view/4451158730/",
    "title": "Senior Angular Developer",
    "company": "Example Software",
    "location": "Wrocław, Dolnośląskie, Poland",
    "workplace_type": "Remote",
    "posted_age": "2 days ago",
    "scouted_at": "2026-08-12T21:40:00+00:00",
}


def test_job_kind_keeps_the_real_url(relay):
    source, queue_path = relay
    queue_path.write_text(json.dumps([JOB_RECORD]), encoding="utf-8")

    jobs = source.search()

    assert len(jobs) == 1
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4451158730/"
    assert not jobs[0].url.startswith(URL_PREFIX)


def test_job_kind_carries_no_post_text(relay):
    """post_text is what reroutes apply through the paste flow — a real job url
    must be FETCHED instead (hunter/services/apply_service.py)."""
    source, queue_path = relay
    queue_path.write_text(json.dumps([JOB_RECORD]), encoding="utf-8")

    job = source.search()[0]

    assert "post_text" not in job.raw


def test_job_kind_sets_no_permalink_duplicate(relay):
    """A permalink equal to url would render the same link twice on the card
    (Job.telegram_text prints both)."""
    source, queue_path = relay
    queue_path.write_text(json.dumps([JOB_RECORD]), encoding="utf-8")

    assert not source.search()[0].raw.get("permalink")


def test_job_kind_takes_title_and_company_from_the_record(relay):
    source, queue_path = relay
    queue_path.write_text(json.dumps([JOB_RECORD]), encoding="utf-8")

    job = source.search()[0]

    assert job.title == "Senior Angular Developer"
    assert job.company == "Example Software"


def test_job_kind_folds_workplace_type_into_location(relay):
    """The central location whitelist reads job.location only — a remote posting
    tagged with a non-whitelisted city has to carry its "Remote" badge there or
    the filter drops it."""
    source, queue_path = relay
    record = {**JOB_RECORD, "location": "Warsaw, Poland", "workplace_type": "Remote"}
    queue_path.write_text(json.dumps([record]), encoding="utf-8")

    assert source.search()[0].location == "Warsaw, Poland (Remote)"


def test_job_kind_does_not_duplicate_an_already_present_badge(relay):
    source, queue_path = relay
    record = {**JOB_RECORD, "location": "Warsaw, Poland (Remote)", "workplace_type": "Remote"}
    queue_path.write_text(json.dumps([record]), encoding="utf-8")

    assert source.search()[0].location == "Warsaw, Poland (Remote)"


def test_job_kind_without_url_is_dropped(relay):
    """An empty url would normalize to the same dedup key for every such row and
    could never be fetched."""
    source, queue_path = relay
    queue_path.write_text(json.dumps([{**JOB_RECORD, "url": ""}]), encoding="utf-8")

    assert source.search() == []


def test_post_kind_is_unchanged_by_the_v2_branch(relay):
    """Absent `kind` means "post" — every v1 payload in flight keeps working."""
    source, queue_path = relay
    queue_path.write_text(
        json.dumps([{"author": "Jane", "body": "We're hiring an Angular dev."}]),
        encoding="utf-8",
    )

    job = source.search()[0]

    assert job.url.startswith(URL_PREFIX)
    assert job.raw["post_text"] == "We're hiring an Angular dev."


def test_mixed_queue_yields_both_kinds(relay):
    source, queue_path = relay
    queue_path.write_text(
        json.dumps([JOB_RECORD, {"author": "Jane", "body": "hiring an Angular dev"}]),
        encoding="utf-8",
    )

    jobs = source.search()

    assert len(jobs) == 2
    assert jobs[0].url == JOB_RECORD["url"]
    assert jobs[1].url.startswith(URL_PREFIX)
