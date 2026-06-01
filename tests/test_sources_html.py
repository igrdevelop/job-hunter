"""Tests for hunter/sources/_html.py — HTML fallback fetcher + URL cleaner."""

from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.html_fallback import clean_url, fetch_html


def test_clean_url_strips_known_tracking_params() -> None:
    url = (
        "https://example.com/jobs/123"
        "?utm_source=newsletter&utm_campaign=hot&trk=abc&keep=me"
    )
    assert clean_url(url) == "https://example.com/jobs/123?keep=me"


def test_clean_url_preserves_url_when_no_tracking_params() -> None:
    url = "https://example.com/jobs/123?id=456&q=python"
    assert clean_url(url) == url


def test_clean_url_handles_url_with_no_query() -> None:
    url = "https://example.com/jobs/123"
    assert clean_url(url) == url


def test_clean_url_drops_query_entirely_when_only_tracking() -> None:
    url = "https://example.com/jobs/123?utm_source=x&fbclid=y"
    assert clean_url(url) == "https://example.com/jobs/123"


def _mk_response(text: str, status: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.text = text
    resp.status_code = status
    resp.raise_for_status = MagicMock()
    return resp


def test_fetch_html_extracts_visible_text() -> None:
    html = (
        "<html><body>"
        "<script>alert('x')</script>"
        "<style>.a {}</style>"
        "<h1>Senior Frontend Developer</h1>"
        "<p>We need someone with deep Angular experience.</p>"
        "<p>" + "Lorem ipsum " * 30 + "</p>"
        "</body></html>"
    )
    with patch("hunter.sources.html_fallback.requests.get", return_value=_mk_response(html)):
        out = fetch_html("https://example.com/x")
    assert "Senior Frontend Developer" in out
    assert "Angular" in out
    assert "alert(" not in out
    assert ".a {}" not in out


def test_fetch_html_raises_when_too_short() -> None:
    with patch(
        "hunter.sources.html_fallback.requests.get",
        return_value=_mk_response("<html><body>tiny</body></html>"),
    ):
        with pytest.raises(ValueError, match="too little text"):
            fetch_html("https://example.com/x")


def test_fetch_html_truncates_long_text() -> None:
    long_body = "Senior Angular role. " * 2000  # well above MAX_TEXT_LEN
    html = f"<html><body><p>{long_body}</p></body></html>"
    with patch("hunter.sources.html_fallback.requests.get", return_value=_mk_response(html)):
        out = fetch_html("https://example.com/x")
    assert out.endswith("[... truncated ...]")
    assert len(out) <= 15_100  # MAX_TEXT_LEN + the truncation marker
