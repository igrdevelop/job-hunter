"""JustRemote source parsing + JSON API handling (no network)."""

from unittest.mock import patch

from hunter.sources.justremote import (
    JustRemoteSource,
    _format_location,
    _slug_from_url,
)


def _sample() -> dict:
    return {
        "id": 23572,
        "title": "Senior Frontend Developer (Angular)",
        "company_name": "Acme",
        "href": "remote-developer-jobs/senior-frontend-developer-angular-acme",
        "category": "developer",
        "remote_type": "Fully Remote",
        "job_country": None,
        "location_restrictions": ["United States"],
        "is_active": True,
    }


def test_parse_minimal() -> None:
    src = JustRemoteSource()
    job = src._parse(_sample())
    assert job is not None
    assert job.title == "Senior Frontend Developer (Angular)"
    assert job.company == "Acme"
    assert job.location == "Fully Remote — United States"
    assert job.url == (
        "https://justremote.co/remote-developer-jobs/"
        "senior-frontend-developer-angular-acme"
    )
    assert job.source == "justremote"


def test_parse_strips_leading_slash_in_href() -> None:
    src = JustRemoteSource()
    raw = _sample()
    raw["href"] = "/remote-developer-jobs/x-acme"
    job = src._parse(raw)
    assert job is not None
    assert job.url == "https://justremote.co/remote-developer-jobs/x-acme"


def test_parse_rejects_inactive_and_incomplete() -> None:
    src = JustRemoteSource()
    inactive = _sample()
    inactive["is_active"] = False
    assert src._parse(inactive) is None
    assert src._parse({"title": "X", "company_name": "Y", "href": ""}) is None
    assert src._parse({"title": "", "company_name": "Y", "href": "z"}) is None


def test_format_location() -> None:
    assert _format_location("Fully Remote", ["United States"]) == "Fully Remote — United States"
    assert _format_location("Fully Remote", None) == "Fully Remote"
    assert _format_location("Fully Remote", []) == "Fully Remote"
    # remote_type without a 'remote' token still gets one appended
    assert _format_location("Flexible", None) == "Flexible (Remote)"
    assert _format_location(None, None) == "Remote"
    assert _format_location("", ["Poland", "Germany"]) == "Remote — Poland, Germany"


def test_slug_from_url() -> None:
    assert (
        _slug_from_url("https://justremote.co/remote-developer-jobs/foo-bar-acme")
        == "foo-bar-acme"
    )
    assert (
        _slug_from_url("https://justremote.co/remote-developer-jobs/foo-bar-acme/")
        == "foo-bar-acme"
    )


def test_matches_url() -> None:
    src = JustRemoteSource()
    assert src.matches_url("https://justremote.co/remote-developer-jobs/x") is True
    assert src.matches_url("https://www.justremote.co/x") is True
    assert src.matches_url("https://remotive.com/x") is False


def _resp(payload):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    return _R()


def test_search_prefilters_by_title() -> None:
    src = JustRemoteSource()
    listing = [
        _sample(),  # Angular -> kept
        {
            "id": 2,
            "title": "Senior ServiceNow Developer",  # no frontend keyword -> dropped
            "company_name": "BackCo",
            "href": "remote-developer-jobs/servicenow-backco",
            "category": "developer",
            "remote_type": "Fully Remote",
            "is_active": True,
        },
    ]
    with patch("hunter.sources.justremote.requests.get", return_value=_resp(listing)):
        jobs = src.search()
    assert [j.title for j in jobs] == ["Senior Frontend Developer (Angular)"]


def test_search_returns_empty_on_error() -> None:
    src = JustRemoteSource()
    with patch("hunter.sources.justremote.requests.get", side_effect=RuntimeError("boom")):
        assert src.search() == []


def test_fetch_text_assembles_sections() -> None:
    src = JustRemoteSource()
    detail = {
        "title": "Senior Frontend Developer",
        "about_role": "<p>Build <b>Angular</b> apps.</p>",
        "who_looking_for": "<ul><li>5y TS</li></ul>",
        "our_offer": "Remote + equity",
        "about_company": "<p>We are Acme.</p>",
    }
    with patch("hunter.sources.justremote.requests.get", return_value=_resp(detail)):
        text = src.fetch_text("https://justremote.co/remote-developer-jobs/x-acme")
    assert "Senior Frontend Developer" in text
    assert "Build Angular apps." in text
    assert "5y TS" in text
    assert "We are Acme." in text
    assert "<" not in text  # HTML stripped


def test_fetch_text_falls_back_on_non_dict() -> None:
    src = JustRemoteSource()
    with patch(
        "hunter.sources.justremote.requests.get", return_value=_resp([1, 2, 3])
    ), patch(
        "hunter.sources.html_fallback.fetch_html", return_value="fallback"
    ) as m_fb:
        text = src.fetch_text("https://justremote.co/remote-developer-jobs/x-acme")
    assert text == "fallback"
    m_fb.assert_called_once()
