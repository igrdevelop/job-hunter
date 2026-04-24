"""4dayweek.io source and URL slug parsing (no network)."""

from hunter.sources.fourdayweek import (
    FourdayweekSource,
    _format_salary,
    _prefilter_context,
)
from job_fetch.fourdayweek import _slug_from_job_url


def _sample_job(**overrides: object) -> dict:
    base = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "slug": "senior-frontend-acme",
        "title": "Senior Frontend Engineer",
        "description": "We use TypeScript and Angular.",
        "url": "https://4dayweek.io/jobs/senior-frontend-acme",
        "category": "Engineering",
        "role": "Software Engineer",
        "level": "Senior",
        "contract_type": "permanent",
        "schedule_type": "4_day_week",
        "work_arrangement": "remote",
        "is_remote": True,
        "office_locations": [],
        "remote_allowed": [{"country": "Poland", "city": "", "continent": ""}],
        "salary_min": 12_000_000,
        "salary_max": 15_000_000,
        "salary_currency": "USD",
        "salary_period": "year",
        "skills": [{"name": "TypeScript", "slug": "typescript"}],
        "stack": [{"name": "Angular", "slug": "angular"}],
        "tools": [],
        "posted_at": "2026-01-15T12:00:00Z",
        "company": {
            "id": "c1",
            "slug": "acme",
            "name": "Acme Corp",
            "url": "https://4dayweek.io/company/acme/jobs",
        },
    }
    base.update(overrides)
    return base


def test_fourdayweek_parse() -> None:
    src = FourdayweekSource()
    job = src._parse(_sample_job())
    assert job is not None
    assert job.title == "Senior Frontend Engineer"
    assert job.company == "Acme Corp"
    assert job.url == "https://4dayweek.io/jobs/senior-frontend-acme"
    assert job.source == "fourdayweek"
    assert "Remote" in job.location
    assert "Poland" in job.location
    assert job.salary == "120 000–150 000 USD/yr"


def test_fourdayweek_parse_hybrid_office() -> None:
    src = FourdayweekSource()
    raw = _sample_job(
        work_arrangement="hybrid",
        is_remote=False,
        office_locations=[{"city": "Berlin", "country": "Germany", "continent": "EU"}],
        remote_allowed=[],
    )
    job = src._parse(raw)
    assert job is not None
    assert "Hybrid" in job.location
    assert "Berlin" in job.location


def test_fourdayweek_parse_incomplete() -> None:
    src = FourdayweekSource()
    assert src._parse(_sample_job(title="")) is None
    assert src._parse(_sample_job(url="")) is None
    assert src._parse(_sample_job(company={})) is None


def test_format_salary_empty() -> None:
    assert _format_salary(_sample_job(salary_min=None, salary_max=None)) is None


def test_format_salary_min_only() -> None:
    assert (
        _format_salary(_sample_job(salary_min=9_000_000, salary_max=None))
        == "90 000+ USD/yr"
    )


def test_prefilter_context_skills() -> None:
    ctx = _prefilter_context(_sample_job())
    assert "TypeScript" in ctx
    assert "Angular" in ctx


def test_slug_from_job_url() -> None:
    assert (
        _slug_from_job_url("https://4dayweek.io/jobs/senior-frontend-acme")
        == "senior-frontend-acme"
    )
    assert (
        _slug_from_job_url("https://www.4dayweek.io/jobs/some-job_slug-1/")
        == "some-job_slug-1"
    )
    assert _slug_from_job_url("https://example.com/jobs/x") is None
