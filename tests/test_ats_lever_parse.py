"""Lever adapter — pure-parse tests (no network)."""

from hunter.ats.lever import parse_lever_job


def test_parse_valid() -> None:
    raw = {
        "id": "u1",
        "text": "Software Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/u1",
        "categories": {"location": "Warsaw, Poland", "team": "Eng"},
    }
    job = parse_lever_job(raw, "acme", "Acme")
    assert job is not None
    assert job.title == "Software Engineer"
    assert job.company == "Acme"
    assert job.source == "ats:lever:acme"
    assert job.url == raw["hostedUrl"]
    assert "Warsaw" in job.location


def test_parse_skips_missing_title() -> None:
    raw = {
        "hostedUrl": "https://jobs.lever.co/x/1",
        "categories": {"location": "EU"},
    }
    assert parse_lever_job(raw, "x", "X") is None


def test_parse_skips_missing_hosted_url() -> None:
    raw = {"text": "Role", "categories": {"location": "Remote"}}
    assert parse_lever_job(raw, "x", "X") is None


def test_parse_defaults_location_remote_when_missing() -> None:
    raw = {
        "text": "PM",
        "hostedUrl": "https://jobs.lever.co/x/1",
        "categories": {},
    }
    job = parse_lever_job(raw, "x", "X")
    assert job is not None
    assert job.location == "Remote"
