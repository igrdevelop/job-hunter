"""Ashby adapter — pure-parse tests (no network)."""

from hunter.ats.ashby import _extract_ashby_salary, parse_ashby_job


def test_parse_valid() -> None:
    raw = {
        "id": "a1",
        "title": "Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/corp/a1",
        "location": "Poland",
        "isRemote": False,
        "isListed": True,
    }
    job = parse_ashby_job(raw, "corp", "Corp")
    assert job is not None
    assert job.title == "Engineer"
    assert job.company == "Corp"
    assert job.source == "ats:ashby:corp"
    assert job.url == raw["jobUrl"]
    assert job.location == "Poland"


def test_parse_skips_unlisted() -> None:
    raw = {
        "title": "Hidden",
        "jobUrl": "https://jobs.ashbyhq.com/x/1",
        "isListed": False,
        "location": "Remote",
    }
    assert parse_ashby_job(raw, "x", "X") is None


def test_parse_skips_missing_job_url() -> None:
    raw = {"title": "Role", "isListed": True, "location": "EU"}
    assert parse_ashby_job(raw, "x", "X") is None


def test_parse_remote_adds_suffix() -> None:
    raw = {
        "title": "Staff Engineer",
        "jobUrl": "https://jobs.ashbyhq.com/linear/u1",
        "location": "Europe",
        "isRemote": True,
        "isListed": True,
    }
    job = parse_ashby_job(raw, "linear", "Linear")
    assert job is not None
    assert "(Remote)" in job.location


def test_extract_salary_from_summary() -> None:
    comp = {
        "scrapeableCompensationSalarySummary": "€100k–€130k",
        "summaryComponents": [],
    }
    assert _extract_ashby_salary(comp) == "€100k–€130k"
