"""Himalayas source parsing (no network)."""

from hunter.sources.himalayas import (
    HimalayasSource,
    _format_location,
    _format_salary,
    _prefilter_context,
)


def _sample_job_dict(**overrides: object) -> dict:
    base = {
        "title": "Senior Frontend Engineer",
        "excerpt": "We need TypeScript and React experience.",
        "companyName": "Stripe",
        "companySlug": "stripe",
        "companyLogo": "https://example.com/logo.png",
        "employmentType": "Full Time",
        "minSalary": 120000,
        "maxSalary": 180000,
        "seniority": ["Senior"],
        "currency": "USD",
        "locationRestrictions": [],
        "timezoneRestrictions": [],
        "categories": ["Software Engineering", "TypeScript"],
        "parentCategories": ["Engineering"],
        "description": "<p>Angular and frontend work.</p>",
        "pubDate": 1740200000000,
        "expiryDate": 1742800000000,
        "applicationLink": "https://stripe.com/jobs/listing/senior-frontend",
        "guid": "stripe-senior-frontend-abc",
    }
    base.update(overrides)
    return base


def test_himalayas_parse() -> None:
    src = HimalayasSource()
    job = src._parse(_sample_job_dict())
    assert job is not None
    assert job.title == "Senior Frontend Engineer"
    assert job.company == "Stripe"
    assert job.location == "Worldwide"
    assert job.salary == "120 000–180 000 USD/yr"
    assert job.url == "https://stripe.com/jobs/listing/senior-frontend"
    assert job.source == "himalayas"


def test_himalayas_parse_location_restrictions() -> None:
    src = HimalayasSource()
    raw = _sample_job_dict(
        locationRestrictions=[
            {"alpha2": "PL", "name": "Poland", "slug": "poland"},
            {"alpha2": "DE", "name": "Germany", "slug": "germany"},
        ],
    )
    job = src._parse(raw)
    assert job is not None
    assert job.location == "Poland, Germany"


def test_himalayas_parse_incomplete() -> None:
    src = HimalayasSource()
    assert src._parse(_sample_job_dict(title="")) is None
    assert src._parse(_sample_job_dict(companyName="")) is None
    assert src._parse(_sample_job_dict(applicationLink="")) is None


def test_format_salary_empty() -> None:
    assert _format_salary(_sample_job_dict(minSalary=None, maxSalary=None)) is None


def test_format_salary_min_only() -> None:
    assert (
        _format_salary(_sample_job_dict(minSalary=90000, maxSalary=None))
        == "90 000+ USD/yr"
    )


def test_format_salary_max_only() -> None:
    assert (
        _format_salary(_sample_job_dict(minSalary=None, maxSalary=70000))
        == "up to 70 000 USD/yr"
    )


def test_format_location_empty_list() -> None:
    assert _format_location(_sample_job_dict()) == "Worldwide"


def test_prefilter_context_includes_categories() -> None:
    raw = _sample_job_dict()
    ctx = _prefilter_context(raw)
    assert "TypeScript" in ctx
    assert "Engineering" in ctx
    assert "Angular" in ctx
