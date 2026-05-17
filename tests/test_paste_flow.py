"""Paste flow: parse_apply_cli_argv (--paste-file) + telegram_bot helpers."""

from apply_agent import parse_apply_cli_argv
from hunter.telegram_bot import _extract_url, _looks_like_paste


# ── apply_agent CLI parsing ──────────────────────────────────────────────────


def test_parse_apply_cli_argv_paste_file_only() -> None:
    url, force_cli, force, full, co, ti, paste_file, notify_start = parse_apply_cli_argv(
        ["apply_agent.py", "--paste-file", "C:/tmp/posting.txt"]
    )
    assert url == ""
    assert paste_file == "C:/tmp/posting.txt"
    assert force_cli is False and force is False and full is False
    assert notify_start is False


def test_parse_apply_cli_argv_paste_file_with_url() -> None:
    url, _, _, _, _, _, paste_file, _ = parse_apply_cli_argv(
        [
            "apply_agent.py",
            "https://example.com/job/123",
            "--paste-file",
            "/tmp/p.txt",
        ]
    )
    assert url == "https://example.com/job/123"
    assert paste_file == "/tmp/p.txt"


def test_parse_apply_cli_argv_backward_compat_url_only() -> None:
    url, _, _, _, _, _, paste_file, _ = parse_apply_cli_argv(
        ["apply_agent.py", "https://example.com/job/123", "--force"]
    )
    assert url == "https://example.com/job/123"
    assert paste_file == ""


def test_parse_apply_cli_argv_notify_start() -> None:
    url, _, _, _, _, _, _, notify_start = parse_apply_cli_argv(
        ["apply_agent.py", "https://example.com/job/1", "--notify-start"]
    )
    assert url == "https://example.com/job/1"
    assert notify_start is True


# ── Telegram paste detection ─────────────────────────────────────────────────


def test_looks_like_paste_short_text_is_not_paste() -> None:
    assert _looks_like_paste("hi") is False


def test_looks_like_paste_single_url_is_not_paste() -> None:
    assert _looks_like_paste("https://justjoin.it/job-offer/acme-frontend-krakow") is False


def test_looks_like_paste_long_text_no_url_is_paste() -> None:
    text = "Senior Angular Developer at Acme. " * 20  # ~700 chars
    assert _looks_like_paste(text) is True


def test_looks_like_paste_url_with_lots_of_text_is_paste() -> None:
    text = (
        "https://example.com/job/1 "
        + "We are looking for a senior frontend engineer with Angular. " * 10
    )
    assert _looks_like_paste(text) is True


def test_looks_like_paste_url_with_minor_surrounding_text_is_not_paste() -> None:
    # URL + a few words = still a URL message, not a paste.
    assert _looks_like_paste("check this out https://example.com/job/1 please") is False


# ── URL extraction ──────────────────────────────────────────────────────────


def test_extract_url_picks_first_https() -> None:
    text = "See https://foo.com/a and https://bar.com/b"
    assert _extract_url(text) == "https://foo.com/a"


def test_extract_url_strips_trailing_punctuation() -> None:
    assert _extract_url("link: https://example.com/x).") == "https://example.com/x"


def test_extract_url_no_url_returns_empty() -> None:
    assert _extract_url("just some job description") == ""
