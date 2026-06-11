"""Tests for fetch_text on NEXT_DATA / cloudscraper sources (Phase 3.2d).

Covers: bulldogjob, solidjobs, theprotocol, pracuj.

These four share JSON-LD / __NEXT_DATA__ / BS4 cascading strategies, so we
test matches_url + at least one successful path + the html_fallback recovery
path per source.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.bulldogjob import BulldogJobSource, _extract_job_id
from hunter.sources.pracuj import (
    PracujSource,
    _extract_archived_notice,
    _format_job_posting_ld as pracuj_format_ld,
)
from hunter.sources.solidjobs import SolidJobsSource
from hunter.sources.theprotocol import TheProtocolSource


def _mk_html_response(text: str, status: int = 200, final_url: str | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    resp.url = final_url or "https://example.com"
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


# ── Bulldogjob ──────────────────────────────────────────────────────────────

def test_bulldogjob_matches_url() -> None:
    s = BulldogJobSource()
    assert s.matches_url("https://bulldogjob.com/companies/jobs/12345")
    assert s.matches_url("https://www.bulldogjob.com/companies/jobs/x")
    assert not s.matches_url("https://example.com/x")


def test_bulldogjob_extract_job_id() -> None:
    assert _extract_job_id("https://bulldogjob.com/companies/jobs/abc-123") == "abc-123"
    assert _extract_job_id("https://bulldogjob.com/companies/jobs/42") == "42"
    with pytest.raises(ValueError):
        _extract_job_id("https://bulldogjob.com/wrong/path")


def test_bulldogjob_fetch_text_from_next_data() -> None:
    apollo = {
        "Company:42": {"name": "ExampleCo"},
        "Job:abc-123": {
            "position": "Senior Angular Engineer",
            "company": {"__ref": "Company:42"},
            "locations": [{"location": {"cityEn": "Wroclaw"}}],
            "remote": True,
            "experienceLevel": "senior",
            "mainTechnology": "Angular",
            "technologyTags": ["RxJS", "TypeScript"],
            "b2bSalary": {"money": "20000", "currency": "PLN", "timeframe": "month"},
            "offer": "<p>Build great apps</p>",
            "requirements": "<p>5+ years Angular</p>",
        },
    }
    next_data = {"props": {"pageProps": {"__APOLLO_STATE__": apollo}}}
    html = (
        f'<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_data)}</script></body></html>"
    )
    with patch(
        "hunter.sources.bulldogjob.requests.get",
        return_value=_mk_html_response(html),
    ):
        out = BulldogJobSource().fetch_text("https://bulldogjob.com/companies/jobs/abc-123")
    assert "Senior Angular Engineer" in out
    assert "ExampleCo" in out
    assert "Wroclaw, Remote" in out
    assert "Main technology: Angular" in out
    assert "20000 PLN/month" in out
    assert "Build great apps" in out
    assert "5+ years Angular" in out


def test_bulldogjob_fetch_text_raises_when_no_next_data() -> None:
    with patch(
        "hunter.sources.bulldogjob.requests.get",
        return_value=_mk_html_response("<html><body>no script here</body></html>"),
    ):
        with pytest.raises(ValueError, match="No __NEXT_DATA__"):
            BulldogJobSource().fetch_text("https://bulldogjob.com/companies/jobs/x")


# ── SolidJobs ───────────────────────────────────────────────────────────────

def test_solidjobs_matches_url() -> None:
    s = SolidJobsSource()
    assert s.matches_url("https://solid.jobs/offer/12345/angular-developer")
    assert s.matches_url("https://www.solid.jobs/o/x")
    assert not s.matches_url("https://example.com/x")


def test_solidjobs_fetch_text_returns_expired_for_not_found_url() -> None:
    out = SolidJobsSource().fetch_text("https://solid.jobs/offer-not-found/123")
    assert out == "Offer expired"


def test_solidjobs_fetch_text_returns_expired_when_redirected() -> None:
    with patch(
        "hunter.sources.solidjobs.requests.get",
        return_value=_mk_html_response(
            "<html><body>not relevant</body></html>",
            final_url="https://solid.jobs/offer-not-found/",
        ),
    ):
        out = SolidJobsSource().fetch_text("https://solid.jobs/offer/123/slug")
    assert out == "Offer expired"


def test_solidjobs_fetch_text_from_json_ld() -> None:
    ld = {
        "@type": "JobPosting",
        "title": "Senior Frontend Developer",
        "hiringOrganization": {"name": "ExampleCo"},
        "jobLocation": {"address": {"addressLocality": "Warsaw", "addressCountry": "PL"}},
        "description": "<p>" + ("Build great stuff. " * 6) + "</p>",
        "employmentType": "FULL_TIME",
    }
    html = (
        '<html><body>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        '</body></html>'
    )
    with patch(
        "hunter.sources.solidjobs.requests.get",
        return_value=_mk_html_response(html, final_url="https://solid.jobs/o/x"),
    ):
        out = SolidJobsSource().fetch_text("https://solid.jobs/offer/123/x")
    assert "Senior Frontend Developer" in out
    assert "ExampleCo" in out
    assert "Warsaw, PL" in out
    assert "Build great stuff" in out


def test_solidjobs_fetch_text_falls_back_when_network_fails() -> None:
    with patch(
        "hunter.sources.solidjobs.requests.get",
        side_effect=Exception("net down"),
    ), patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="fallback ok",
    ) as m:
        out = SolidJobsSource().fetch_text("https://solid.jobs/offer/123/x")
    assert out == "fallback ok"
    m.assert_called_once()


# ── theprotocol ─────────────────────────────────────────────────────────────

def test_theprotocol_matches_url() -> None:
    s = TheProtocolSource()
    assert s.matches_url("https://theprotocol.it/szczegoly/praca/x,oferta,abc")
    assert s.matches_url("https://www.theprotocol.it/praca/x,oferta,abc")
    assert not s.matches_url("https://example.com/x")


def test_theprotocol_fetch_text_from_json_ld() -> None:
    ld = {
        "@type": "JobPosting",
        "title": "Senior Angular Engineer",
        "hiringOrganization": {"name": "ExampleCo"},
        "description": "<p>" + ("Do cool things. " * 8) + "</p>",
    }
    html = (
        '<html><body>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        '</body></html>'
    )
    with patch(
        "hunter.sources.theprotocol._scraper.get",
        return_value=_mk_html_response(html),
    ):
        out = TheProtocolSource().fetch_text("https://theprotocol.it/praca/x,oferta,abc")
    assert "Senior Angular Engineer" in out
    assert "ExampleCo" in out
    assert "Do cool things" in out


def test_theprotocol_fetch_text_from_next_data_offer() -> None:
    """Detail pages carry the body in __NEXT_DATA__.props.pageProps.offer."""
    next_data = {
        "props": {
            "pageProps": {
                "offer": {
                    "language": "pl",
                    "attributes": {
                        "title": {"value": "Frontend Developer (K/M)"},
                        "employer": {"name": "Polska Grupa Lotnicza"},
                        "workplaces": [{"location": "Warszawa, Włochy", "city": "Warszawa"}],
                        "employment": {
                            "detailedWorkModes": [{"name": "praca zdalna"}],
                        },
                    },
                    "textSections": [
                        {
                            "type": "responsibilities",
                            "plainText": "Projektowanie i implementacja nowych funkcjonalności.",
                            "elements": [],
                        },
                        {
                            "type": "requirements-expected",
                            "plainText": "Bardzo dobra znajomość frameworka Angular oraz RxJS.",
                            "elements": [],
                        },
                    ],
                }
            }
        }
    }
    html = (
        '<html><body><script id="__NEXT_DATA__" type="application/json">'
        f"{json.dumps(next_data)}</script></body></html>"
    )
    with patch(
        "hunter.sources.theprotocol._scraper.get",
        return_value=_mk_html_response(html),
    ):
        out = TheProtocolSource().fetch_text("https://theprotocol.it/praca/x,oferta,abc")
    assert "Frontend Developer (K/M)" in out
    assert "Polska Grupa Lotnicza" in out
    assert "Warszawa" in out
    assert "Responsibilities" in out
    assert "Projektowanie i implementacja" in out
    assert "Requirements" in out
    assert "znajomość frameworka Angular" in out
    assert len(out) > 100


def test_theprotocol_fetch_text_falls_back_on_network_error() -> None:
    with patch(
        "hunter.sources.theprotocol._scraper.get",
        side_effect=Exception("blocked"),
    ), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback ok",
    ) as m:
        out = TheProtocolSource().fetch_text("https://theprotocol.it/x")
    assert out == "fallback ok"
    m.assert_called_once()


# ── Pracuj ──────────────────────────────────────────────────────────────────

def test_pracuj_matches_url() -> None:
    s = PracujSource()
    assert s.matches_url("https://www.pracuj.pl/praca/x,oferta,12345")
    assert s.matches_url("https://it.pracuj.pl/x")
    assert not s.matches_url("https://example.com/x")


def test_pracuj_extract_archived_notice() -> None:
    archived = '<html><body><div data-test="section-archived">closed</div></body></html>'
    assert "zakończył" in _extract_archived_notice(archived)
    assert _extract_archived_notice("<html><body>active</body></html>") == ""


def test_pracuj_format_ld_skips_when_no_description() -> None:
    # A JSON-LD without description should yield empty (forces fallthrough)
    assert pracuj_format_ld({"title": "x"}) == ""


def test_pracuj_fetch_text_from_json_ld() -> None:
    ld = {
        "@type": "JobPosting",
        "title": "Senior Frontend",
        "hiringOrganization": {"name": "ExampleCo"},
        "description": "<p>" + ("Cool gig. " * 12) + "</p>",
    }
    html = (
        '<html><body>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        '</body></html>'
    )
    with patch(
        "hunter.sources.pracuj._scraper.get",
        return_value=_mk_html_response(html),
    ):
        out = PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")
    assert "Senior Frontend" in out
    assert "ExampleCo" in out


def test_pracuj_fetch_text_appends_archived_notice() -> None:
    ld = {
        "@type": "JobPosting",
        "title": "Some role",
        "hiringOrganization": {"name": "Co"},
        "description": "<p>" + ("Stuff. " * 12) + "</p>",
    }
    html = (
        '<html><body>'
        f'<script type="application/ld+json">{json.dumps(ld)}</script>'
        '<div data-test="section-archived">closed</div>'
        '</body></html>'
    )
    with patch(
        "hunter.sources.pracuj._scraper.get",
        return_value=_mk_html_response(html),
    ):
        out = PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")
    assert "Some role" in out
    assert "zakończył" in out


def test_pracuj_fetch_text_falls_back_through_all_strategies() -> None:
    # cloudscraper raises → plain requests (dynamically imported inside _fetch_detail_html)
    # also raises → final fetch_html fallback wins.
    with patch(
        "hunter.sources.pracuj._scraper.get",
        side_effect=Exception("blocked"),
    ), patch(
        "requests.get",
        side_effect=Exception("plain blocked too"),
    ), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback ok",
    ) as m:
        out = PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")
    assert out == "fallback ok"
    m.assert_called_once()


# ── Pracuj 429 backoff ────────────────────────────────────────────────────────

def _http_error(status: int, retry_after: str | None = None) -> Exception:
    """Build a requests-style HTTPError carrying a response with a status code."""
    import requests as _req

    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Retry-After": retry_after} if retry_after else {}
    return _req.exceptions.HTTPError(f"HTTP {status}", response=resp)


def test_pracuj_429_retries_then_raises_without_fallback() -> None:
    """Persistent 429 retries the cloudscraper session, then raises (no html_fallback)."""
    from hunter.sources.pracuj import _RATE_LIMIT_MAX_RETRIES

    scraper_get = MagicMock(side_effect=_http_error(429))
    with patch("hunter.sources.pracuj._scraper.get", scraper_get), patch(
        "hunter.sources.pracuj.time.sleep"
    ) as sleep_mock, patch(
        "requests.get", side_effect=AssertionError("plain requests must not run on 429")
    ), patch(
        "hunter.sources.html_fallback.fetch_html",
        side_effect=AssertionError("html_fallback must not run on 429"),
    ):
        with pytest.raises(Exception) as exc_info:
            PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")

    assert getattr(exc_info.value.response, "status_code", None) == 429
    assert scraper_get.call_count == _RATE_LIMIT_MAX_RETRIES + 1
    assert sleep_mock.call_count == _RATE_LIMIT_MAX_RETRIES


def test_pracuj_429_then_success_returns_text() -> None:
    """A transient 429 that clears on retry yields the recovered page."""
    ld = {
        "@type": "JobPosting",
        "title": "Recovered Role",
        "hiringOrganization": {"name": "Co"},
        "description": "<p>" + ("Body. " * 12) + "</p>",
    }
    html = f'<html><body><script type="application/ld+json">{json.dumps(ld)}</script></body></html>'
    scraper_get = MagicMock(
        side_effect=[_http_error(429), _mk_html_response(html)]
    )
    with patch("hunter.sources.pracuj._scraper.get", scraper_get), patch(
        "hunter.sources.pracuj.time.sleep"
    ):
        out = PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")

    assert "Recovered Role" in out
    assert scraper_get.call_count == 2


def test_pracuj_non_429_error_still_falls_back() -> None:
    """A non-429 cloudscraper failure keeps the existing plain-requests/html_fallback path."""
    with patch(
        "hunter.sources.pracuj._scraper.get", side_effect=Exception("blocked")
    ), patch("requests.get", side_effect=Exception("plain blocked too")), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback ok"
    ) as m:
        out = PracujSource().fetch_text("https://www.pracuj.pl/praca/x,oferta,abc")
    assert out == "fallback ok"
    m.assert_called_once()


def test_pracuj_retry_after_header_honored() -> None:
    """Retry-After header drives the backoff delay."""
    from hunter.sources.pracuj import _retry_after_delay

    err = _http_error(429, retry_after="7")
    assert _retry_after_delay(err, attempt=0) == 7.0
