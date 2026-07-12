"""Tests for the shared source text/location helpers."""

from hunter.sources.text_utils import REMOTE_ANY, ensure_remote_token, strip_html


# --- strip_html ----------------------------------------------------------


def test_strip_html_removes_tags_and_unescapes() -> None:
    html = "<p>Senior <b>Angular</b> &amp; React</p>"
    assert strip_html(html, 100) == "Senior Angular & React"


def test_strip_html_collapses_whitespace_and_newlines() -> None:
    html = "<div>line one</div>\n\n<div>line   two</div>"
    assert strip_html(html, 100) == "line one line two"


def test_strip_html_truncates_to_max_len() -> None:
    assert strip_html("<p>abcdefghij</p>", 4) == "abcd"


def test_strip_html_empty_and_non_string_inputs() -> None:
    assert strip_html("", 100) == ""
    assert strip_html(None, 100) == ""
    assert strip_html(123, 100) == ""  # type: ignore[arg-type]


def test_strip_html_tag_spanning_newline() -> None:
    # `[^>]+` already spans newlines, so a multi-line tag is stripped whole.
    assert strip_html("<a\nhref='x'>link</a>", 100) == "link"


# --- ensure_remote_token -------------------------------------------------


def test_ensure_remote_token_empty_becomes_remote() -> None:
    assert ensure_remote_token("") == "Remote"
    assert ensure_remote_token(None) == "Remote"
    assert ensure_remote_token("   ") == "Remote"


def test_ensure_remote_token_appends_when_missing() -> None:
    assert ensure_remote_token("Berlin") == "Berlin (Remote)"
    assert ensure_remote_token("USA, Canada") == "USA, Canada (Remote)"


def test_ensure_remote_token_keeps_existing_token() -> None:
    assert ensure_remote_token("Fully Remote") == "Fully Remote"
    assert ensure_remote_token("REMOTE") == "REMOTE"


def test_ensure_remote_token_appends_geo_hint() -> None:
    assert ensure_remote_token("Fully Remote", "United States") == "Fully Remote — United States"
    assert (
        ensure_remote_token("Flexible", "Poland, Germany") == "Flexible (Remote) — Poland, Germany"
    )
    assert ensure_remote_token("", "Poland") == "Remote — Poland"


def test_ensure_remote_token_blank_geo_ignored() -> None:
    assert ensure_remote_token("Berlin", "") == "Berlin (Remote)"
    assert ensure_remote_token("Berlin", "   ") == "Berlin (Remote)"


def test_remote_any_membership() -> None:
    assert "worldwide" in REMOTE_ANY
    assert "anywhere" in REMOTE_ANY
    assert "remote" in REMOTE_ANY
    assert "berlin" not in REMOTE_ANY
