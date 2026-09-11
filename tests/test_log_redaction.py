"""The Telegram bot token must never reach a log file.

python-telegram-bot calls `api.telegram.org/bot<TOKEN>/<method>` and httpx logs
the full URL at INFO, so every long-poll (one every ~10 s) wrote the live token
into `logs/hunter_errors.log` — which `scheduled_gdrive_upload_logs` uploads to
Google Drive daily, and which `docker compose logs` prints on demand. Found on
2026-09-10 while checking the container after the non-root switch.

Two defences, both pinned here: httpx/httpcore are silenced to WARNING (those
lines are noise anyway), and `RedactBotToken` scrubs the pattern out of
whatever any other logger might print.
"""

from __future__ import annotations

import logging

import pytest

from hunter.__main__ import RedactBotToken, _setup_logging

REAL_SHAPED = (
    "https://api.telegram.org/bot8687246460:AAEovwrSje4iR-zIORmi5sg189kNaOV9dzw/getUpdates"
)


def _record(msg: str, args=None) -> logging.LogRecord:
    return logging.LogRecord("t", logging.INFO, "p", 1, msg, args, None)


def test_redacts_a_token_carried_in_args() -> None:
    """httpx passes the URL through record.args, not record.msg — scrubbing
    only the message would miss the real case entirely."""
    rec = _record('HTTP Request: POST %s "%s"', (REAL_SHAPED, "HTTP/1.1 200 OK"))
    assert RedactBotToken().filter(rec) is True
    out = rec.getMessage()
    assert "AAEovwrSje4iR-zIORmi5sg189kNaOV9dzw" not in out
    assert "<redacted>" in out
    assert "HTTP/1.1 200 OK" in out


def test_redacts_a_token_inside_the_message() -> None:
    rec = _record(f"see {REAL_SHAPED} for details")
    RedactBotToken().filter(rec)
    assert "AAEovwrSje4iR-zIORmi5sg189kNaOV9dzw" not in rec.getMessage()


def test_keeps_the_bot_id_prefix() -> None:
    """The numeric id is not a secret and identifies which bot logged — only
    the part after the colon is dropped."""
    rec = _record(f"see {REAL_SHAPED}")
    RedactBotToken().filter(rec)
    assert "bot8687246460:<redacted>" in rec.getMessage()


def test_redacts_a_token_in_dict_args() -> None:
    # logging unwraps a single mapping argument into record.args, so the dict
    # has to be handed over inside a tuple the way a real caller would.
    rec = _record("%(url)s", ({"url": REAL_SHAPED},))
    RedactBotToken().filter(rec)
    assert "AAEovwrSje4iR-zIORmi5sg189kNaOV9dzw" not in rec.getMessage()


def test_leaves_ordinary_lines_alone() -> None:
    rec = _record("hunt finished: %d new jobs", (7,))
    RedactBotToken().filter(rec)
    assert rec.getMessage() == "hunt finished: 7 new jobs"


@pytest.mark.parametrize(
    "text",
    [
        "robots.txt says nothing",
        "bot42:short",  # too short to be a token
        "https://example.com/robot/1234567890",
    ],
)
def test_does_not_mangle_unrelated_text(text: str) -> None:
    rec = _record(text)
    RedactBotToken().filter(rec)
    assert rec.getMessage() == text


def test_setup_logging_silences_httpx_and_attaches_the_filter(tmp_path, monkeypatch) -> None:
    import hunter.__main__ as main_mod

    monkeypatch.setattr(main_mod, "PROJECT_DIR", tmp_path)
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_httpx = logging.getLogger("httpx").level
    try:
        root.handlers = []
        _setup_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpcore").level == logging.WARNING
        assert root.handlers, "expected handlers to be installed"
        for handler in root.handlers:
            assert any(isinstance(f, RedactBotToken) for f in handler.filters), (
                f"{handler!r} has no token-redaction filter"
            )
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        logging.getLogger("httpx").setLevel(saved_httpx)
        logging.getLogger("httpcore").setLevel(saved_httpx)
