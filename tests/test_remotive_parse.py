"""Remotive source parsing (no network)."""

from hunter.sources.remotive import RemotiveSource, _format_location


def test_remotive_parse_minimal() -> None:
    src = RemotiveSource()
    raw = {
        "title": "Senior Frontend Engineer",
        "company_name": "Acme",
        "url": "https://remotive.com/remote-jobs/123",
        "candidate_required_location": "Worldwide",
        "salary": "$100k",
        "description": "<p>Angular and TypeScript</p>",
        "tags": ["angular"],
    }
    job = src._parse(raw)
    assert job is not None
    assert job.title == "Senior Frontend Engineer"
    assert job.company == "Acme"
    assert job.location == "Remote"
    assert job.salary == "$100k"
    assert job.url == "https://remotive.com/remote-jobs/123"
    assert job.source == "remotive"
    assert job.raw == raw


def test_remotive_parse_rejects_incomplete() -> None:
    src = RemotiveSource()
    assert src._parse({"title": "X", "company_name": "", "url": "http://x"}) is None


def test_format_location_region() -> None:
    assert _format_location({"candidate_required_location": "USA"}) == "USA (Remote)"
    assert _format_location({}) == "Remote"
