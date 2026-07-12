"""Himalayas source parsing (no network)."""

from unittest.mock import patch

from hunter.sources.himalayas import (
    HimalayasSource,
    _company_slug_from_url,
    _format_location,
    _format_salary,
    _prefilter_context,
    _title_query_from_url,
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


def test_matches_url() -> None:
    src = HimalayasSource()
    assert src.matches_url("https://himalayas.app/companies/x/jobs/y") is True
    assert src.matches_url("https://www.himalayas.app/companies/x/jobs/y") is True
    assert src.matches_url("https://example.com/x") is False


def test_company_slug_from_url() -> None:
    url = "https://himalayas.app/companies/about-source/jobs/lead-frontend-dev"
    assert _company_slug_from_url(url) == "about-source"
    assert _company_slug_from_url("https://himalayas.app/companies/") == ""
    assert _company_slug_from_url("https://example.com/other/path") == ""


def test_fetch_text_returns_description_for_matching_application_link() -> None:
    src = HimalayasSource()
    url = "https://himalayas.app/companies/about-source/jobs/lead-frontend-dev"
    api_response = {
        "jobs": [
            _sample_job_dict(applicationLink="https://himalayas.app/companies/x/jobs/other"),
            _sample_job_dict(
                applicationLink=url,
                description="<p>Right <b>one</b></p>",
            ),
        ]
    }

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return api_response

    with patch("hunter.sources.himalayas.requests.get", return_value=_Resp()):
        text = src.fetch_text(url)
    assert text == "Right one"


def test_fetch_text_falls_back_when_no_application_link_match() -> None:
    src = HimalayasSource()
    url = "https://himalayas.app/companies/about-source/jobs/missing"
    api_response = {"jobs": [_sample_job_dict(applicationLink="https://himalayas.app/other")]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return api_response

    with patch(
        "hunter.sources.himalayas.requests.get", return_value=_Resp()
    ), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback"
    ) as m_fb:
        text = src.fetch_text(url)
    assert text == "fallback"
    m_fb.assert_called_once()


def test_fetch_text_retries_with_title_query_when_company_page_1_misses() -> None:
    """A big agency's company-only page 1 doesn't have the target job, but
    the q= retry (relevance-ranked by title) does — live-verified against a
    real 359-listing company on himalayas.app."""
    src = HimalayasSource()
    url = "https://himalayas.app/companies/bigagency/jobs/front-end-developer-4409560950"
    miss_response = {"jobs": [_sample_job_dict(applicationLink="https://himalayas.app/other")]}
    hit_response = {
        "jobs": [
            _sample_job_dict(applicationLink=url, description="<p>Found via q</p>"),
        ]
    }

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    calls: list[dict] = []

    def _fake_get(_api_url, params, headers, timeout):
        calls.append(params)
        return _Resp(hit_response if "q" in params else miss_response)

    with patch("hunter.sources.himalayas.requests.get", side_effect=_fake_get):
        text = src.fetch_text(url)
    assert text == "Found via q"
    assert len(calls) == 2
    assert "q" not in calls[0]
    assert calls[1]["q"] == "front end developer"


def test_title_query_from_url() -> None:
    url = "https://himalayas.app/companies/thehivecareers/jobs/front-end-developer-4409560950"
    assert _title_query_from_url(url) == "front end developer"

    no_id_url = "https://himalayas.app/companies/about-source/jobs/lead-frontend-dev"
    assert _title_query_from_url(no_id_url) == "lead frontend dev"

    assert _title_query_from_url("https://himalayas.app/companies/x/") == ""
    assert _title_query_from_url("https://example.com/other/path") == ""


def test_fetch_text_falls_back_when_no_company_slug_in_url() -> None:
    src = HimalayasSource()
    url = "https://himalayas.app/jobs/some-legacy-path"
    with patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback"
    ) as m_fb:
        text = src.fetch_text(url)
    assert text == "fallback"
    m_fb.assert_called_once()
