"""Working Nomads source parsing + URL handling (no network)."""

from unittest.mock import patch

from hunter.sources.workingnomads import (
    WorkingNomadsSource,
    _format_location,
    _prefilter_context,
    _slug_from_url,
)


def _sample_source() -> dict:
    return {
        "title": "Senior Frontend Developer (Angular)",
        "company": "ViaBill",
        "slug": "senior-frontend-developer-angular-viabill-1641531",
        "category_name": "Development",
        "tags": ["angular", "typescript"],
        "all_tags": [],
        "locations": ["Europe"],
        "salary_range_short": "$80k-$100k",
        "description": "<p>We need <strong>Angular</strong> and TypeScript.</p>",
        "expired": False,
    }


def test_parse_minimal() -> None:
    src = WorkingNomadsSource()
    job = src._parse(_sample_source())
    assert job is not None
    assert job.title == "Senior Frontend Developer (Angular)"
    assert job.company == "ViaBill"
    assert job.location == "Europe (Remote)"
    assert job.salary == "$80k-$100k"
    assert job.url == (
        "https://www.workingnomads.com/jobs/senior-frontend-developer-angular-viabill-1641531"
    )
    assert job.source == "workingnomads"


def test_parse_rejects_incomplete() -> None:
    src = WorkingNomadsSource()
    assert src._parse({"title": "X", "company": "Y", "slug": ""}) is None
    assert src._parse({"title": "", "company": "Y", "slug": "z"}) is None
    assert src._parse({"title": "X", "company": "", "slug": "z"}) is None


def test_format_location() -> None:
    assert _format_location(["Europe"]) == "Europe (Remote)"
    assert _format_location(["USA", "Canada"]) == "USA, Canada (Remote)"
    assert _format_location(["Anywhere"]) == "Remote"
    assert _format_location(["Worldwide"]) == "Remote"
    assert _format_location([]) == "Remote"
    assert _format_location(None) == "Remote"
    assert _format_location("USA") == "USA (Remote)"


def test_slug_from_url() -> None:
    assert _slug_from_url("https://www.workingnomads.com/jobs/abc-123") == "abc-123"
    assert _slug_from_url("https://www.workingnomads.com/jobs/abc-123/") == "abc-123"
    assert _slug_from_url("https://www.workingnomads.com/about") == ""


def test_prefilter_context_includes_category_and_tags() -> None:
    ctx = _prefilter_context(_sample_source()).lower()
    assert "development" in ctx
    assert "angular" in ctx
    assert "typescript" in ctx


def test_matches_url() -> None:
    src = WorkingNomadsSource()
    assert src.matches_url("https://www.workingnomads.com/jobs/x") is True
    assert src.matches_url("https://workingnomads.com/jobs/x") is True
    assert src.matches_url("https://remotive.com/x") is False
    assert src.matches_url("https://example.com/x") is False


def test_fetch_text_returns_description_for_matching_slug() -> None:
    src = WorkingNomadsSource()
    slug = "senior-frontend-developer-angular-viabill-1641531"
    es_response = {
        "hits": {
            "hits": [
                {"_source": {"slug": "other-slug", "description": "<p>wrong</p>"}},
                {"_source": {"slug": slug, "description": "<p>Right <b>one</b></p>"}},
            ]
        }
    }

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return es_response

    with patch("hunter.sources.workingnomads.requests.post", return_value=_Resp()):
        text = src.fetch_text(f"https://www.workingnomads.com/jobs/{slug}")
    assert text == "Right one"


def test_fetch_text_falls_back_when_no_slug_match() -> None:
    src = WorkingNomadsSource()
    es_response = {"hits": {"hits": [{"_source": {"slug": "nope", "description": "x"}}]}}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return es_response

    with (
        patch("hunter.sources.workingnomads.requests.post", return_value=_Resp()),
        patch("hunter.sources.html_fallback.fetch_html", return_value="fallback") as m_fb,
    ):
        text = src.fetch_text("https://www.workingnomads.com/jobs/missing-slug")
    assert text == "fallback"
    m_fb.assert_called_once()
