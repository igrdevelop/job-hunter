"""Tests for hunter/sources/thesmartjobs.py (Smart Jobs / thesmartjobs.com)."""

from unittest.mock import MagicMock, patch

from hunter.expired_check import is_job_expired
from hunter.sources.thesmartjobs import (
    TheSmartJobsSource,
    _format_location,
    _format_salary,
    _slug_from_url,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

LISTING_JOB = {
    "id": "019f5a3f-7dc7-7142-ba0a-4c211265b29a",
    "title": "Frontend Developer/ka",
    "slug": "programista-frontend-mid-9044bedb",
    "slugUrl": "praca/programista-frontend-mid-9044bedb",
    "language": "pl",
    "workModes": ["remote"],
    "locations": [{"city": "Warszawa", "country": "Polska", "displayName": "Warszawa, Polska"}],
    "salaries": [
        {"contractType": "b2b", "currency": "PLN", "min": 14000, "max": 18000, "period": "monthly"},
    ],
    "company": {"name": "SYZYGY Warsaw", "slug": "syzygy"},
    "role": {"name": "Frontend Developer"},
    "attributes": [{"attributeName": "Angular", "groupName": "Technologies"}],
    "description": "<p>We build things with <strong>Angular</strong>.</p>",
}

DETAIL_JOB = {
    "id": "019f5a3f-7dc7-7142-ba0a-4c211265b29a",
    "title": "Frontend Developer/ka",
    "status": "published",
    "company": {"name": "SYZYGY Warsaw"},
    "applicationFormUrl": "https://syzygy.traffit.com/public/form/xyz",
    "workModes": ["remote"],
    "locations": [{"city": "Warszawa", "country": "Polska"}],
    "salaries": [
        {"contractType": "b2b", "currency": "PLN", "min": 14000, "max": 18000, "period": "monthly"},
    ],
    "description": "<p><strong>Senior Angular</strong></p><p>Build a design system.</p>",
    "slugUrl": "praca/programista-frontend-mid-9044bedb",
}


def _mock_response(payload: dict, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ── search() ─────────────────────────────────────────────────────────────────


def test_search_parses_listing_and_builds_public_url() -> None:
    src = TheSmartJobsSource()
    with (
        patch(
            "hunter.sources.thesmartjobs.requests.get",
            return_value=_mock_response({"data": [LISTING_JOB], "meta": {"total": 1}}),
        ),
        patch("hunter.sources.thesmartjobs.time.sleep"),
    ):
        jobs = src.search()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.url == "https://thesmartjobs.com/en/praca/programista-frontend-mid-9044bedb"
    assert job.company == "SYZYGY Warsaw"
    assert job.source == "thesmartjobs"
    assert "remote" in job.location.lower()
    assert "14 000" in (job.salary or "")
    assert "B2B" in (job.salary or "")


def test_search_dedups_across_queries() -> None:
    src = TheSmartJobsSource()
    with (
        patch(
            "hunter.sources.thesmartjobs.requests.get",
            return_value=_mock_response({"data": [LISTING_JOB]}),
        ) as m,
        patch("hunter.sources.thesmartjobs.time.sleep"),
    ):
        jobs = src.search()
    assert m.call_count == 3  # 3 queries
    assert len(jobs) == 1  # same job each time -> one Job


def test_search_skips_non_matching_titles() -> None:
    raw = dict(LISTING_JOB, title="Senior Accountant", role={"name": "Accountant"})
    raw["attributes"] = [{"attributeName": "Excel"}]
    raw["description"] = "<p>Bookkeeping.</p>"
    src = TheSmartJobsSource()
    with (
        patch(
            "hunter.sources.thesmartjobs.requests.get",
            return_value=_mock_response({"data": [raw]}),
        ),
        patch("hunter.sources.thesmartjobs.time.sleep"),
    ):
        jobs = src.search()
    assert jobs == []


def test_search_falls_back_to_slug_when_sluburl_missing() -> None:
    raw = dict(LISTING_JOB)
    raw.pop("slugUrl")
    src = TheSmartJobsSource()
    job = src._parse(raw)
    assert job is not None
    assert job.url == "https://thesmartjobs.com/en/praca/programista-frontend-mid-9044bedb"


def test_search_survives_network_error() -> None:
    src = TheSmartJobsSource()
    with (
        patch("hunter.sources.thesmartjobs.requests.get", side_effect=OSError("boom")),
        patch("hunter.sources.thesmartjobs.time.sleep"),
    ):
        assert src.search() == []


# ── matches_url / fetch_text ─────────────────────────────────────────────────


def test_matches_url_claims_thesmartjobs_only() -> None:
    src = TheSmartJobsSource()
    assert src.matches_url("https://thesmartjobs.com/en/praca/frontend-123")
    assert not src.matches_url("https://findmyremote.ai/companies/x/jobs/y")
    assert not src.matches_url("https://nofluffjobs.com/pl/job/x")


def test_slug_from_url() -> None:
    assert (
        _slug_from_url("https://thesmartjobs.com/en/praca/programista-frontend-mid-9044bedb")
        == "programista-frontend-mid-9044bedb"
    )
    assert _slug_from_url("https://thesmartjobs.com/en/praca/x-1/") == "x-1"
    assert _slug_from_url("https://thesmartjobs.com/") == ""


def test_fetch_text_returns_description_via_api() -> None:
    src = TheSmartJobsSource()
    with patch(
        "hunter.sources.thesmartjobs.requests.get",
        return_value=_mock_response(DETAIL_JOB),
    ):
        text = src.fetch_text("https://thesmartjobs.com/en/praca/programista-frontend-mid-9044bedb")
    assert "Senior Angular" in text
    assert "Build a design system." in text
    assert "SYZYGY Warsaw" in text
    assert "traffit.com" in text  # applicationFormUrl surfaced in the header


def test_fetch_text_deleted_job_reads_as_expired() -> None:
    """HTTP 404 -> synthetic text the expired-check recognizes (EXPIRED, not FAIL)."""
    src = TheSmartJobsSource()
    with patch(
        "hunter.sources.thesmartjobs.requests.get",
        return_value=_mock_response({"error": "Job not found"}, status=404),
    ):
        text = src.fetch_text("https://thesmartjobs.com/en/praca/gone-123")
    assert is_job_expired(text)


def test_fetch_text_closed_status_reads_as_expired() -> None:
    closed = dict(DETAIL_JOB, status="closed")
    src = TheSmartJobsSource()
    with patch(
        "hunter.sources.thesmartjobs.requests.get",
        return_value=_mock_response(closed),
    ):
        text = src.fetch_text("https://thesmartjobs.com/en/praca/closed-1")
    assert is_job_expired(text)


def test_fetch_text_falls_back_to_html_on_api_failure() -> None:
    src = TheSmartJobsSource()
    with (
        patch("hunter.sources.thesmartjobs.requests.get", side_effect=OSError("api down")),
        patch("hunter.sources.html_fallback.fetch_html", return_value="fallback text") as m_fb,
    ):
        text = src.fetch_text("https://thesmartjobs.com/en/praca/slug-1")
    assert text == "fallback text"
    m_fb.assert_called_once()


def test_dispatcher_routes_thesmartjobs_urls() -> None:
    from hunter.sources import fetch_job_text

    with patch(
        "hunter.sources.thesmartjobs.TheSmartJobsSource.fetch_text",
        return_value="smartjobs payload",
    ) as m:
        out = fetch_job_text("https://thesmartjobs.com/en/praca/slug-9?utm_source=tg")
    assert out == "smartjobs payload"
    m.assert_called_once()


# ── location / salary formatting ─────────────────────────────────────────────


def test_format_location_remote_keeps_remote_token() -> None:
    loc = _format_location({"workModes": ["remote"], "locations": [{"city": "Wrocław"}]})
    assert "remote" in loc.lower()
    assert "Wrocław" in loc


def test_format_location_onsite_city_passes_through() -> None:
    # On-site Warszawa: no remote token injected — central filter drops it.
    loc = _format_location({"workModes": ["onsite"], "locations": [{"city": "Warszawa"}]})
    assert loc == "Warszawa"
    assert "remote" not in loc.lower()


def test_format_location_empty() -> None:
    assert _format_location({"workModes": [], "locations": []}) == ""


def test_format_salary_range() -> None:
    s = _format_salary(
        {"salaries": [{"contractType": "b2b", "currency": "PLN", "min": 14000, "max": 18000}]}
    )
    assert s == "14 000–18 000 PLN B2B"


def test_format_salary_none_when_absent() -> None:
    assert _format_salary({"salaries": []}) is None
    assert _format_salary({}) is None
