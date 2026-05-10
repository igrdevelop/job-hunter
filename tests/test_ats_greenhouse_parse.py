"""Greenhouse adapter — pure-parse tests (no network)."""

from hunter.ats.greenhouse import parse_greenhouse_job


def test_parse_valid() -> None:
    raw = {
        "id": 1,
        "title": "Senior Frontend Engineer",
        "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/123",
        "company_name": "GitLab",
        "location": {"name": "Remote"},
    }
    job = parse_greenhouse_job(raw, "gitlab", "GitLab")
    assert job is not None
    assert job.title == "Senior Frontend Engineer"
    assert job.company == "GitLab"
    assert job.source == "ats:greenhouse:gitlab"
    assert job.url == raw["absolute_url"]
    assert job.location == "Remote"


def test_parse_skips_missing_title() -> None:
    raw = {
        "absolute_url": "https://example.com/j/1",
        "location": {"name": "Berlin"},
    }
    assert parse_greenhouse_job(raw, "acme", "Acme") is None


def test_parse_skips_missing_url() -> None:
    raw = {"title": "Engineer", "location": {"name": "Paris"}}
    assert parse_greenhouse_job(raw, "acme", "Acme") is None


def test_parse_location_remote_in_name() -> None:
    raw = {
        "title": "Dev",
        "absolute_url": "https://boards.example/j/1",
        "location": {"name": "EMEA Remote"},
    }
    job = parse_greenhouse_job(raw, "x", "X")
    assert job is not None
    assert "remote" in job.location.lower()


def test_parse_empty_location() -> None:
    raw = {
        "title": "Dev",
        "absolute_url": "https://job-boards.greenhouse.io/x/j/1",
        "location": {},
    }
    job = parse_greenhouse_job(raw, "x", "X Corp")
    assert job is not None
    assert job.location == "Unknown"
