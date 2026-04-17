"""
Tests for hunter/sources/inhire.py — parser, pre-filter, and URL builder.

All tests are unit tests (no Playwright / no network) using fixture dicts
that simulate the two data shapes the scraper produces:
  - Vuex store shape   (direct store.state fields)
  - DOM fallback shape (only 'href' + '_text')
"""

import json
from pathlib import Path

import pytest

from hunter.sources.inhire import InhireSource

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


src = InhireSource()


# ---------------------------------------------------------------------------
# _parse — Vuex shapes
# ---------------------------------------------------------------------------

class TestParseVuexShapes:
    def test_basic_vuex_fixture(self):
        raw = _load("inhire_vuex_offer.json")
        job = src._parse(raw)
        assert job is not None
        assert job.title == "Angular Engineer"
        assert job.company == "Inhire Labs"
        assert job.url == "https://app.inhire.io/oferty-pracy/angular-engineer,oferta,123456"
        assert job.source == "inhire"

    def test_dict_company_and_dict_location(self):
        """company and location as nested dicts — common in richer Vuex payloads."""
        raw = _load("inhire_vuex_offer_dict_company.json")
        job = src._parse(raw)
        assert job is not None
        assert job.company == "Inhire Corp"
        assert "Wrocław" in job.location
        assert "(Hybrid)" in job.location

    def test_dict_salary_range(self):
        raw = _load("inhire_vuex_offer_dict_company.json")
        job = src._parse(raw)
        assert job is not None
        assert job.salary == "20000-28000 PLN"

    def test_remote_only_no_city(self):
        """fullyRemote=true, no city → location should be 'Remote'."""
        raw = _load("inhire_vuex_offer_remote_only.json")
        job = src._parse(raw)
        assert job is not None
        assert job.location == "Remote"
        assert job.title == "Angular Tech Lead"

    def test_salary_display_text_string(self):
        raw = _load("inhire_vuex_offer_remote_only.json")
        job = src._parse(raw)
        assert job is not None
        assert job.salary == "25 000 - 35 000 PLN"

    def test_url_strips_query_params(self):
        raw = {
            "name": "Frontend Dev",
            "company": "Acme",
            "offerUrl": "https://app.inhire.io/oferty-pracy/foo,oferta,1?utm_source=test&ref=abc",
            "city": "Kraków",
        }
        job = src._parse(raw)
        assert job is not None
        assert "?" not in job.url
        assert "utm_source" not in job.url

    def test_relative_url_is_prefixed(self):
        raw = {
            "name": "Frontend Dev",
            "company": "Acme",
            "offerUrl": "/oferty-pracy/foo,oferta,2",
            "city": "Kraków",
        }
        job = src._parse(raw)
        assert job is not None
        assert job.url.startswith("https://app.inhire.io")

    def test_missing_title_returns_none(self):
        raw = {"company": "NoTitle Corp", "offerUrl": "https://app.inhire.io/x,oferta,3"}
        assert src._parse(raw) is None

    def test_missing_url_returns_none(self):
        raw = {"name": "Some Job", "company": "NoUrl Corp"}
        assert src._parse(raw) is None


# ---------------------------------------------------------------------------
# _parse — DOM fallback shape (old format with href + _text only)
# ---------------------------------------------------------------------------

class TestParseDomFallback:
    def test_dom_fixture_old_format(self):
        """Old format: href + _text only, no structured fields."""
        raw = _load("inhire_dom_offer.json")
        job = src._parse(raw)
        assert job is not None
        assert job.title == "Junior Angular Developer"
        assert "?" not in job.url
        assert job.url.startswith("https://app.inhire.io")

    def test_dom_text_multiline_title_old_format(self):
        raw = {
            "href": "/oferty-pracy/angular-dev,oferta,555",
            "_text": "Angular Developer\nSome Company\nWrocław",
            "title": "",
            "company": "",
        }
        job = src._parse(raw)
        assert job is not None
        assert job.title == "Angular Developer"


# ---------------------------------------------------------------------------
# _parse — new DOM format (structured fields extracted by updated _extract_dom)
# ---------------------------------------------------------------------------

class TestParseDomNew:
    def test_new_dom_fixture(self):
        """New format: structured title/company/salary/location extracted from card DOM."""
        raw = _load("inhire_dom_offer_new.json")
        job = src._parse(raw)
        assert job is not None
        assert job.title == "Senior Angular Developer"
        assert job.company == "Acme Software"
        assert job.salary == "20 000 - 26 000 PLN / miesiąc"
        assert job.location == "Remote within Poland"
        assert job.url == "https://app.inhire.io/praca/senior-angular-developer-remote-wroclaw-job-arbeit-196946"
        assert job.source == "inhire"

    def test_new_dom_url_is_absolute(self):
        raw = _load("inhire_dom_offer_new.json")
        job = src._parse(raw)
        assert job is not None
        assert job.url.startswith("https://app.inhire.io/praca/")

    def test_new_dom_no_query_params(self):
        raw = dict(_load("inhire_dom_offer_new.json"))
        raw["url"] = "/praca/angular-dev-job-arbeit-111?ref=list&utm_source=x"
        job = src._parse(raw)
        assert job is not None
        assert "?" not in job.url


# ---------------------------------------------------------------------------
# _is_relevant — pre-filter
# ---------------------------------------------------------------------------

class TestIsRelevant:
    def _make_raw(self, text: str = "") -> dict:
        return {"_text": text}

    def _make_job(self, title: str):
        from hunter.models import Job
        return Job(
            title=title,
            company="X",
            location="Remote",
            salary=None,
            url="https://app.inhire.io/x,oferta,1",
            source="inhire",
        )

    def test_angular_title_is_relevant(self):
        job = self._make_job("Senior Angular Developer")
        assert src._is_relevant(self._make_raw(), job) is True

    def test_frontend_title_is_relevant(self):
        job = self._make_job("Frontend Engineer")
        assert src._is_relevant(self._make_raw(), job) is True

    def test_java_title_excluded(self):
        job = self._make_job("Senior Java Developer")
        assert src._is_relevant(self._make_raw(), job) is False

    def test_backend_title_excluded(self):
        job = self._make_job("Backend Developer")
        assert src._is_relevant(self._make_raw(), job) is False

    def test_vue_title_excluded(self):
        job = self._make_job("Vue.js Developer")
        assert src._is_relevant(self._make_raw(), job) is False

    def test_unrelated_title_with_angular_in_text_is_relevant(self):
        job = self._make_job("Engineer")
        assert src._is_relevant(self._make_raw("angular frontend project"), job) is True

    def test_unrelated_title_no_keyword_text_is_not_relevant(self):
        job = self._make_job("Engineer")
        assert src._is_relevant(self._make_raw("python data platform"), job) is False


# ---------------------------------------------------------------------------
# _build_url
# ---------------------------------------------------------------------------

class TestBuildUrl:
    @pytest.mark.parametrize("key", ["offerUrl", "url", "offerAbsoluteUri", "href"])
    def test_all_url_keys_recognized(self, key: str):
        raw = {key: "https://app.inhire.io/oferty-pracy/foo,oferta,1"}
        result = InhireSource._build_url(raw)
        assert result == "https://app.inhire.io/oferty-pracy/foo,oferta,1"

    def test_empty_raw_returns_empty(self):
        assert InhireSource._build_url({}) == ""
