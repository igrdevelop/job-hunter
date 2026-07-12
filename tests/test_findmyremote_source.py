"""Tests for hunter/sources/findmyremote.py (FindMyRemote.ai JSON API source)."""

from unittest.mock import MagicMock, patch

from hunter.expired_check import is_job_expired
from hunter.sources.findmyremote import (
    FindMyRemoteSource,
    _format_location,
    _job_slug_from_url,
)

# ── Fixtures ─────────────────────────────────────────────────────────────────

LISTING_JOB = {
    "id": 188765528,
    "slug": "senior-angular-typescript-engineer-fully-remote-188765528",
    "url": "https://handymaninteractive.teamtailor.com/jobs/8055835-senior-angular",
    "title": "Senior Angular (Typescript) Engineer - Fully Remote",
    "createdAt": "2026-07-11T12:55:47.361218+00:00",
    "employmentTypes": None,
    "countries": ["nz"],
    "skills": ["angular", "typescript", "firebase"],
    "company": {"id": 17776, "name": "HI Technology & Innovation", "slug": "hi-technology"},
}

DETAIL_JOB = {
    "id": 188765528,
    "title": "Senior Angular (Typescript) Engineer - Fully Remote",
    "company": {"name": "HI Technology & Innovation", "slug": "hi-technology"},
    "url": "https://handymaninteractive.teamtailor.com/jobs/8055835-senior-angular",
    "description": "<p><strong>Senior Angular Engineer</strong></p><p>Build things.</p>",
    "dateDeleted": None,
    "countries": ["nz"],
}


def _mock_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


# ── search() ─────────────────────────────────────────────────────────────────


def test_search_parses_listing_and_uses_external_url() -> None:
    src = FindMyRemoteSource()
    with (
        patch(
            "hunter.sources.findmyremote.requests.get",
            return_value=_mock_response({"totalCount": 1, "jobs": [LISTING_JOB]}),
        ),
        patch("hunter.sources.findmyremote.time.sleep"),
    ):
        jobs = src.search()
    assert len(jobs) == 1
    job = jobs[0]
    assert job.url.startswith("https://handymaninteractive.teamtailor.com/")
    assert job.company == "HI Technology & Innovation"
    assert job.source == "findmyremote"
    assert "remote" in job.location.lower()
    assert "NZ" in job.location


def test_search_dedups_across_queries() -> None:
    src = FindMyRemoteSource()
    with (
        patch(
            "hunter.sources.findmyremote.requests.get",
            return_value=_mock_response({"totalCount": 1, "jobs": [LISTING_JOB]}),
        ) as m,
        patch("hunter.sources.findmyremote.time.sleep"),
    ):
        jobs = src.search()
    # 3 queries, same job each time -> one Job
    assert m.call_count == 3
    assert len(jobs) == 1


def test_search_falls_back_to_site_url_when_no_external_link() -> None:
    raw = dict(LISTING_JOB, url="")
    src = FindMyRemoteSource()
    job = src._parse(raw)
    assert job is not None
    assert job.url == (
        "https://findmyremote.ai/companies/hi-technology/jobs/"
        "senior-angular-typescript-engineer-fully-remote-188765528"
    )


def test_search_skips_non_matching_titles() -> None:
    raw = dict(LISTING_JOB, title="Senior Accountant", skills=["excel"])
    src = FindMyRemoteSource()
    with (
        patch(
            "hunter.sources.findmyremote.requests.get",
            return_value=_mock_response({"totalCount": 1, "jobs": [raw]}),
        ),
        patch("hunter.sources.findmyremote.time.sleep"),
    ):
        jobs = src.search()
    assert jobs == []


def test_search_survives_network_error() -> None:
    src = FindMyRemoteSource()
    with (
        patch(
            "hunter.sources.findmyremote.requests.get",
            side_effect=OSError("boom"),
        ),
        patch("hunter.sources.findmyremote.time.sleep"),
    ):
        assert src.search() == []


# ── matches_url / fetch_text ─────────────────────────────────────────────────


def test_matches_url_claims_findmyremote_only() -> None:
    src = FindMyRemoteSource()
    assert src.matches_url("https://findmyremote.ai/companies/x/jobs/some-slug-1")
    assert not src.matches_url("https://jobs.lever.co/x/123")
    assert not src.matches_url("https://himalayas.app/companies/x/jobs/y")


def test_job_slug_from_url() -> None:
    assert (
        _job_slug_from_url(
            "https://findmyremote.ai/companies/miratech-1/jobs/team-lead-frontend-engineer-186862551"
        )
        == "team-lead-frontend-engineer-186862551"
    )
    assert _job_slug_from_url("https://findmyremote.ai/jobs/some-slug-42") == "some-slug-42"
    assert _job_slug_from_url("https://findmyremote.ai/companies/miratech-1") == ""


def test_fetch_text_returns_description_via_api() -> None:
    src = FindMyRemoteSource()
    with patch(
        "hunter.sources.findmyremote.requests.get",
        return_value=_mock_response({"job": DETAIL_JOB}),
    ):
        text = src.fetch_text(
            "https://findmyremote.ai/companies/hi-technology/jobs/"
            "senior-angular-typescript-engineer-fully-remote-188765528"
        )
    assert "Senior Angular Engineer" in text
    assert "Build things." in text
    assert "HI Technology & Innovation" in text
    # Original external apply link surfaced in the text header
    assert "handymaninteractive.teamtailor.com" in text


def test_fetch_text_deleted_job_reads_as_expired() -> None:
    """dateDeleted -> synthetic text the expired-check recognizes (EXPIRED, not FAIL)."""
    deleted = dict(DETAIL_JOB, dateDeleted="2026-07-09T22:37:16.62+00:00")
    src = FindMyRemoteSource()
    with patch(
        "hunter.sources.findmyremote.requests.get",
        return_value=_mock_response({"job": deleted}),
    ):
        text = src.fetch_text("https://findmyremote.ai/companies/x/jobs/gone-123")
    assert is_job_expired(text)


def test_fetch_text_falls_back_to_html_on_api_failure() -> None:
    src = FindMyRemoteSource()
    with (
        patch(
            "hunter.sources.findmyremote.requests.get",
            side_effect=OSError("api down"),
        ),
        patch(
            "hunter.sources.html_fallback.fetch_html",
            return_value="fallback text",
        ) as m_fb,
    ):
        text = src.fetch_text("https://findmyremote.ai/companies/x/jobs/slug-1")
    assert text == "fallback text"
    m_fb.assert_called_once()


def test_dispatcher_routes_findmyremote_urls() -> None:
    from hunter.sources import fetch_job_text

    with patch(
        "hunter.sources.findmyremote.FindMyRemoteSource.fetch_text",
        return_value="fmr payload",
    ) as m:
        out = fetch_job_text("https://findmyremote.ai/companies/x/jobs/slug-9?utm_source=tg")
    assert out == "fmr payload"
    m.assert_called_once()


# ── location formatting ──────────────────────────────────────────────────────


def test_format_location_maps_gated_countries_to_names() -> None:
    loc = _format_location({"countries": ["ru", "pl", "de"]})
    assert "Russia" in loc  # listing-level russia gate matches by name, not ISO code
    assert "Poland" in loc
    assert "DE" in loc
    assert "remote" in loc.lower()


def test_format_location_empty_countries() -> None:
    assert _format_location({"countries": None}) == "Remote"


def test_format_location_caps_long_country_lists() -> None:
    loc = _format_location({"countries": [f"c{i}" for i in range(12)]})
    assert "+4 more" in loc
