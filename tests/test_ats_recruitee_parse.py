"""Recruitee adapter — pure-parse tests (no network)."""

from hunter.ats.recruitee import parse_recruitee_job


def test_parse_valid() -> None:
    raw = {
        "id": 1,
        "title": "Full Stack Developer",
        "location": "Kraków, Poland",
        "country_code": "PL",
        "careers_url": "https://acme.recruitee.com/o/abc",
    }
    job = parse_recruitee_job(raw, "acme", "Acme")
    assert job is not None
    assert job.title == "Full Stack Developer"
    assert job.company == "Acme"
    assert job.source == "ats:recruitee:acme"
    assert job.url == raw["careers_url"]
    assert "Kraków" in job.location


def test_parse_remote_recruitment_suffix() -> None:
    raw = {
        "title": "Support",
        "location": "Berlin",
        "careers_url": "https://x.recruitee.com/o/1",
        "remote_recruitment": True,
    }
    job = parse_recruitee_job(raw, "x", "X")
    assert job is not None
    assert "(Remote)" in job.location


def test_parse_skips_missing_title() -> None:
    raw = {"careers_url": "https://x.recruitee.com/o/1", "location": "NYC"}
    assert parse_recruitee_job(raw, "x", "X") is None


def test_parse_skips_missing_careers_url() -> None:
    raw = {"title": "Role", "location": "EU"}
    assert parse_recruitee_job(raw, "x", "X") is None
