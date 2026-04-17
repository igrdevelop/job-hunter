"""
Tests for hunter/sources/jobleads.py — parser, pre-filter, card extractor.

All tests are unit tests (no network) using fixture dicts and a minimal HTML
fragment that mirrors the real jobleads.com listing page structure.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.jobleads import JobLeadsSource

FIXTURES = Path(__file__).parent / "fixtures" / "sources"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


src = JobLeadsSource()


# ---------------------------------------------------------------------------
# _parse_cards — HTML → list[dict]
# ---------------------------------------------------------------------------

class TestParseCards:
    def setup_method(self):
        html = (FIXTURES / "jobleads_listing.html").read_text(encoding="utf-8")
        self.cards = JobLeadsSource._parse_cards(html)

    def test_finds_correct_count(self):
        assert len(self.cards) == 3

    def test_first_card_title(self):
        assert self.cards[0]["title"] == "Angular Developer \u2013 Hybrid, Training & Growth"

    def test_first_card_url_absolute(self):
        assert self.cards[0]["url"].startswith("https://www.jobleads.com/pl/job/")

    def test_first_card_url_no_query(self):
        assert "?" not in self.cards[0]["url"]

    def test_first_card_company(self):
        assert self.cards[0]["company"] == "Capgemini"

    def test_first_card_location(self):
        assert self.cards[0]["location"] == "Wroc\u0142aw"

    def test_first_card_work_type(self):
        assert self.cards[0]["work_type"].lower() == "hybrid"

    def test_first_card_salary_detected(self):
        assert "PLN" in self.cards[0]["salary"]

    def test_second_card_remote(self):
        assert self.cards[1]["work_type"].lower() == "remote"

    def test_second_card_no_salary(self):
        assert self.cards[1]["salary"] == ""


# ---------------------------------------------------------------------------
# _parse — raw dict → Job
# ---------------------------------------------------------------------------

class TestParse:
    def test_wroclaw_card(self):
        raw = _load("jobleads_card_wroclaw.json")
        job = src._parse(raw)
        assert job is not None
        assert job.title == "Angular Developer \u2013 Hybrid, Training & Growth"
        assert job.company == "Capgemini"
        assert job.salary == "PLN 168,000 - 275,000"
        assert job.source == "jobleads"
        assert job.url.startswith("https://www.jobleads.com")

    def test_wroclaw_hybrid_location(self):
        raw = _load("jobleads_card_wroclaw.json")
        job = src._parse(raw)
        assert job is not None
        assert "Hybrid" in job.location
        assert "Wroc" in job.location

    def test_remote_card(self):
        raw = _load("jobleads_card_remote.json")
        job = src._parse(raw)
        assert job is not None
        assert "Remote" in job.location
        assert job.salary is None

    def test_missing_title_returns_none(self):
        raw = dict(_load("jobleads_card_wroclaw.json"))
        raw["title"] = ""
        assert src._parse(raw) is None

    def test_missing_url_returns_none(self):
        raw = dict(_load("jobleads_card_wroclaw.json"))
        raw["url"] = ""
        assert src._parse(raw) is None

    def test_relative_url_returns_none(self):
        raw = dict(_load("jobleads_card_wroclaw.json"))
        raw["url"] = "/pl/job/something"
        assert src._parse(raw) is None


# ---------------------------------------------------------------------------
# _build_location
# ---------------------------------------------------------------------------

class TestBuildLocation:
    @pytest.mark.parametrize("work_type,city,expected", [
        ("Remote",  "Poland",  "Poland (Remote)"),
        ("remote",  "",        "Remote"),
        ("Hybrid",  "Wrocław", "Wrocław (Hybrid)"),
        ("hybrid",  "",        "Hybrid"),
        ("On-site", "Kraków",  "Kraków"),
        ("",        "Warszawa","Warszawa"),
        ("",        "",        "Unknown"),
    ])
    def test_combinations(self, work_type, city, expected):
        raw = {"location": city, "work_type": work_type}
        assert JobLeadsSource._build_location(raw) == expected


# ---------------------------------------------------------------------------
# _is_relevant — pre-filter
# ---------------------------------------------------------------------------

class TestIsRelevant:
    def _make_job(self, title: str):
        from hunter.models import Job
        return Job(
            title=title, company="X", location="Remote",
            salary=None,
            url="https://www.jobleads.com/pl/job/x--y--z",
            source="jobleads",
        )

    def test_angular_title_passes(self):
        job = self._make_job("Senior Angular Developer")
        assert src._is_relevant({}, job) is True

    def test_frontend_title_passes(self):
        job = self._make_job("Frontend Engineer")
        assert src._is_relevant({}, job) is True

    def test_java_title_blocked(self):
        job = self._make_job("Java Backend Developer")
        assert src._is_relevant({}, job) is False

    def test_vue_title_blocked(self):
        job = self._make_job("Vue.js Developer")
        assert src._is_relevant({}, job) is False

    def test_keyword_in_text_passes(self):
        job = self._make_job("Engineer")
        assert src._is_relevant({"_text": "angular project"}, job) is True

    def test_no_keyword_anywhere_blocked(self):
        job = self._make_job("Engineer")
        assert src._is_relevant({"_text": "python data platform"}, job) is False


# ---------------------------------------------------------------------------
# search() — mock HTTP
# ---------------------------------------------------------------------------

class TestSearch:
    def test_search_returns_filtered_jobs(self):
        html = (FIXTURES / "jobleads_listing.html").read_text(encoding="utf-8")

        with patch("hunter.sources.jobleads._scraper") as mock_scraper:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html
            mock_resp.raise_for_status = MagicMock()
            mock_scraper.get.return_value = mock_resp

            jobs = src.search()

        # Java Backend Developer should be filtered out
        titles = [j.title for j in jobs]
        assert all("Java" not in t for t in titles)
        # Angular and Frontend jobs should pass
        assert any("Angular" in t for t in titles)

    def test_search_deduplicates_across_urls(self):
        html = (FIXTURES / "jobleads_listing.html").read_text(encoding="utf-8")

        with patch("hunter.sources.jobleads._scraper") as mock_scraper:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.text = html
            mock_resp.raise_for_status = MagicMock()
            mock_scraper.get.return_value = mock_resp

            jobs = src.search()

        urls = [j.url for j in jobs]
        assert len(urls) == len(set(urls))

    def test_search_handles_http_error_gracefully(self):
        with patch("hunter.sources.jobleads._scraper") as mock_scraper:
            mock_scraper.get.side_effect = Exception("connection refused")
            jobs = src.search()

        assert jobs == []
