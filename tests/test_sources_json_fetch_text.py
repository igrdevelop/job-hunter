"""Tests for fetch_text on JSON-API-based sources (Phase 3.2c).

Covers: justjoin, nofluffjobs, himalayas, fourdayweek.

For each source we test:
  * matches_url positive / negative cases
  * fetch_text successful API response → formatted text
  * fetch_text error paths (fallback / raise)
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.fourdayweek import FourdayweekSource, _slug_from_job_url
from hunter.sources.himalayas import HimalayasSource
from hunter.sources.justjoin import JustJoinSource
from hunter.sources.nofluffjobs import NoFluffJobsSource


# ── helpers ────────────────────────────────────────────────────────────────

def _mk_json_response(payload, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.json.return_value = payload
    resp.raise_for_status = MagicMock()
    if status >= 400:
        resp.raise_for_status.side_effect = Exception(f"HTTP {status}")
    return resp


# ── JustJoin ────────────────────────────────────────────────────────────────

def test_justjoin_matches_url() -> None:
    s = JustJoinSource()
    assert s.matches_url("https://justjoin.it/job-offer/example-role-warsaw")
    assert s.matches_url("https://www.justjoin.it/offers/abc")
    assert not s.matches_url("https://example.com/jobs/1")


def test_justjoin_fetch_text_formats_api_payload() -> None:
    payload = {
        "title": "Senior Angular Engineer",
        "companyName": "ExampleCo",
        "city": "Warsaw",
        "workplaceType": "remote",
        "experienceLevel": "senior",
        "skills": [{"name": "Angular", "level": 5}, {"name": "TypeScript", "level": 4}],
        "employmentTypes": [{"from": 18000, "to": 24000, "currency": "PLN", "type": "b2b"}],
        "body": "<p>Build Angular apps</p><br><li>Lead architecture</li>",
        "isActive": True,
    }
    with patch(
        "hunter.sources.justjoin.requests.get",
        return_value=_mk_json_response(payload),
    ):
        out = JustJoinSource().fetch_text(
            "https://justjoin.it/job-offer/example-senior-angular-warsaw"
        )
    assert "Senior Angular Engineer" in out
    assert "ExampleCo" in out
    assert "Warsaw" in out
    assert "Angular (5)" in out
    assert "TypeScript (4)" in out
    assert "18000–24000 PLN B2B" in out
    assert "Build Angular apps" in out
    assert "- Lead architecture" in out  # <li> stripping
    assert "<p>" not in out  # HTML stripped


def test_justjoin_fetch_text_marks_inactive_offers() -> None:
    payload = {
        "title": "x", "companyName": "y", "city": "Wroclaw",
        "workplaceType": "office", "body": "<p>" + ("desc " * 50) + "</p>",
        "isActive": False,
    }
    with patch(
        "hunter.sources.justjoin.requests.get",
        return_value=_mk_json_response(payload),
    ):
        out = JustJoinSource().fetch_text(
            "https://justjoin.it/job-offer/x-y-wroclaw"
        )
    assert "Offer expired" in out


def test_justjoin_fetch_text_raises_on_bad_url() -> None:
    with pytest.raises(ValueError, match="extract JustJoin slug"):
        JustJoinSource().fetch_text("https://justjoin.it/random/path")


# ── NoFluffJobs ─────────────────────────────────────────────────────────────

def test_nofluffjobs_matches_url() -> None:
    s = NoFluffJobsSource()
    assert s.matches_url("https://nofluffjobs.com/pl/job/example-slug")
    assert s.matches_url("https://www.nofluffjobs.com/job/foo")
    assert not s.matches_url("https://justjoin.it/x")


def test_nofluffjobs_fetch_text_formats_api_payload() -> None:
    payload = {
        "title": "Senior Angular Developer",
        "name": "ExampleCo",
        "location": {"places": [{"city": "Warsaw"}]},
        "fullyRemote": False,
        "seniority": ["senior"],
        "requirements": {
            "musts": [{"value": "Angular"}, {"value": "TypeScript"}],
            "nices": [{"value": "RxJS"}],
        },
        "essentials": {"salary": {"from": 15000, "to": 25000, "currency": "PLN", "type": "b2b"}},
        "sections": {
            "description": "<p>" + ("Build something cool. " * 8) + "</p>",
            "responsibilities": "Ship code",
        },
    }
    with patch(
        "hunter.sources.nofluffjobs.requests.get",
        return_value=_mk_json_response(payload),
    ):
        out = NoFluffJobsSource().fetch_text(
            "https://nofluffjobs.com/pl/job/example-slug"
        )
    assert "Senior Angular Developer" in out
    assert "ExampleCo" in out
    assert "Warsaw" in out
    assert "Must-have: Angular, TypeScript" in out
    assert "Nice-to-have: RxJS" in out
    assert "15000–25000 PLN b2b" in out
    assert "Build something cool" in out


def test_nofluffjobs_fetch_text_falls_back_on_api_failure() -> None:
    with patch(
        "hunter.sources.nofluffjobs.requests.get",
        side_effect=Exception("network down"),
    ), patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="fallback content",
    ) as m:
        out = NoFluffJobsSource().fetch_text(
            "https://nofluffjobs.com/pl/job/example-slug"
        )
    assert out == "fallback content"
    m.assert_called_once()


# ── Himalayas ───────────────────────────────────────────────────────────────

def test_himalayas_matches_url() -> None:
    s = HimalayasSource()
    assert s.matches_url("https://himalayas.app/companies/x/jobs/y")
    assert s.matches_url("https://www.himalayas.app/jobs/x")
    assert not s.matches_url("https://example.com/x")


def test_himalayas_fetch_text_uses_html_fallback() -> None:
    with patch("hunter.sources.html_fallback.fetch_html", return_value="ok") as m:
        out = HimalayasSource().fetch_text("https://himalayas.app/x")
    assert out == "ok"
    m.assert_called_once_with("https://himalayas.app/x")


# ── 4dayweek ────────────────────────────────────────────────────────────────

def test_fourdayweek_matches_url() -> None:
    s = FourdayweekSource()
    assert s.matches_url("https://4dayweek.io/remote-jobs/angular-developer-abc")
    assert s.matches_url("https://www.4dayweek.io/jobs/xyz")
    assert not s.matches_url("https://example.com/x")


def test_fourdayweek_slug_extraction() -> None:
    assert _slug_from_job_url("https://4dayweek.io/jobs/senior-fe-eng-abc123") == "senior-fe-eng-abc123"
    assert _slug_from_job_url("https://www.4dayweek.io/remote-jobs/abc") == "abc"
    assert _slug_from_job_url("https://4dayweek.io/") is None
    assert _slug_from_job_url("https://example.com/jobs/abc") is None


def test_fourdayweek_fetch_text_formats_api_payload() -> None:
    payload = {
        "title": "Senior Frontend Engineer",
        "company": {"name": "ExampleCo"},
        "url": "https://4dayweek.io/jobs/abc",
        "work_arrangement": "Fully remote",
        "is_remote": True,
        "office_locations": [{"city": "Berlin", "country": "Germany"}],
        "salary_min": 8000000,  # 80 000 in major units
        "salary_max": 12000000,  # 120 000
        "salary_currency": "EUR",
        "salary_period": "year",
        "level": "Senior",
        "skills": [{"name": "Angular"}, {"name": "TypeScript"}],
        "description": "Build amazing things.",
    }
    with patch(
        "hunter.sources.fourdayweek.requests.get",
        return_value=_mk_json_response(payload),
    ):
        out = FourdayweekSource().fetch_text("https://4dayweek.io/jobs/abc")
    assert "Senior Frontend Engineer" in out
    assert "ExampleCo" in out
    assert "Berlin, Germany" in out
    assert "80 000–120 000 EUR/yr" in out
    assert "Tags: Angular, TypeScript" in out
    assert "Build amazing things." in out


def test_fourdayweek_fetch_text_falls_back_on_404() -> None:
    with patch(
        "hunter.sources.fourdayweek.requests.get",
        return_value=_mk_json_response({"error": "not found"}, status=404),
    ), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="html-fallback",
    ) as m:
        out = FourdayweekSource().fetch_text("https://4dayweek.io/jobs/abc")
    assert out == "html-fallback"
    m.assert_called_once()


def test_fourdayweek_fetch_text_falls_back_on_bad_url() -> None:
    with patch(
        "hunter.sources.html_fallback.fetch_html", return_value="html-fallback",
    ) as m:
        out = FourdayweekSource().fetch_text("https://4dayweek.io/")
    assert out == "html-fallback"
    m.assert_called_once()
