"""
Tests for Telegram notification delivery robustness (owner report 2026-07-11).

Telegram rejects the WHOLE message (400 "can't parse entities") when
interpolated content — an LLM error snippet, a quoted posting line — breaks
HTML parsing. `apply_shared.notify` used to ignore the response status and
`bot.notifications._tg_notify` swallowed the BadRequest, so failure
notifications silently vanished: the owner saw a bare "apply_agent failed"
with no reason. Both now resend once as plain text; the bot-side failure
message also escapes the raw stderr/stdout tail it embeds in <pre>.
"""

from __future__ import annotations

import asyncio


# ── apply_shared.notify (subprocess side, requests-based) ─────────────────────


class _Resp:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


def _patch_notify_env(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_CHAT_ID", "123")

    def _fake_post(url, json=None, timeout=None):  # noqa: A002 — mirrors requests API
        calls.append(json)
        # First call fails HTML parsing, any retry succeeds.
        return _Resp(400) if len(calls) == 1 else _Resp(200)

    monkeypatch.setattr("hunter.apply_shared.requests.post", _fake_post)
    return calls


def test_notify_sends_once_on_success(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_CHAT_ID", "123")
    monkeypatch.setattr(
        "hunter.apply_shared.requests.post",
        lambda url, json=None, timeout=None: calls.append(json) or _Resp(200),
    )

    from hunter.apply_shared import notify

    notify("✅ <b>Docs ready!</b>")

    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "HTML"


def test_notify_resends_plain_when_telegram_rejects_html(monkeypatch) -> None:
    calls = _patch_notify_env(monkeypatch)

    from hunter.apply_shared import notify

    # Realistic: llm_client embeds the raw model output in the error string —
    # a stray "<" breaks Telegram's HTML entity parsing.
    notify('❌ <b>LLM failed</b>\n<pre>Could not parse JSON: {"a": <raw…</pre>')

    assert len(calls) == 2
    retry = calls[1]
    assert "parse_mode" not in retry
    assert "<b>" not in retry["text"]
    assert "<pre>" not in retry["text"]
    # The actual diagnostic content survives the tag strip.
    assert "Could not parse JSON" in retry["text"]
    assert "LLM failed" in retry["text"]


def test_notify_retry_strips_anchor_tags_but_keeps_stray_brackets(monkeypatch) -> None:
    calls = _patch_notify_env(monkeypatch)

    from hunter.apply_shared import notify

    notify('📁 <a href="https://drive.google.com/x">Open folder</a> — error: a < b')

    assert len(calls) == 2
    assert "Open folder" in calls[1]["text"]
    assert "<a " not in calls[1]["text"]
    assert "a < b" in calls[1]["text"]  # non-tag content untouched


# ── bot.notifications._tg_notify (bot side, python-telegram-bot) ──────────────


class _FakeBot:
    calls: list[dict] = []

    def __init__(self, token: str) -> None:
        pass

    async def __aenter__(self) -> "_FakeBot":
        return self

    async def __aexit__(self, *args) -> bool:  # noqa: ANN002
        return False

    async def send_message(self, **kwargs) -> None:  # noqa: ANN003
        _FakeBot.calls.append(kwargs)
        if kwargs.get("parse_mode") is not None:
            from telegram.error import BadRequest

            raise BadRequest("Can't parse entities")


def test_tg_notify_resends_plain_on_bad_request(monkeypatch) -> None:
    _FakeBot.calls = []
    monkeypatch.setattr("hunter.bot.notifications.Bot", _FakeBot)

    from hunter.bot.notifications import _tg_notify

    asyncio.run(_tg_notify("❌ <b>apply_agent failed</b>\n<pre>tail with <weird> markup</pre>"))

    assert len(_FakeBot.calls) == 2
    retry = _FakeBot.calls[1]
    assert retry.get("parse_mode") is None
    assert "<b>" not in retry["text"]
    assert "apply_agent failed" in retry["text"]


def test_tg_notify_sends_once_when_html_accepted(monkeypatch) -> None:
    _FakeBot.calls = []

    class _OkBot(_FakeBot):
        async def send_message(self, **kwargs) -> None:  # noqa: ANN003
            _FakeBot.calls.append(kwargs)

    monkeypatch.setattr("hunter.bot.notifications.Bot", _OkBot)

    from hunter.bot.notifications import _tg_notify

    asyncio.run(_tg_notify("✅ <b>ok</b>"))

    assert len(_FakeBot.calls) == 1


# ── apply_runner failure message escapes the raw output tail ──────────────────


def test_run_apply_agent_escapes_error_detail_in_failure_message(monkeypatch) -> None:
    sent: list[str] = []

    async def _fake_run_for_url(**kwargs):  # noqa: ANN003
        return "fail", "[apply_agent] LLM ERROR: got <raw> & partial JSON"

    async def _fake_tg_notify(text: str) -> None:
        sent.append(text)

    monkeypatch.setattr("hunter.services.apply_service.run_apply_agent_for_url", _fake_run_for_url)
    monkeypatch.setattr("hunter.bot.apply_runner._tg_notify", _fake_tg_notify)

    from hunter.bot.apply_runner import _run_apply_agent

    asyncio.run(_run_apply_agent("https://example.com/jobs/esc"))

    assert len(sent) == 1
    msg = sent[0]
    assert "apply_agent failed" in msg
    # The raw tail is escaped so Telegram's HTML parser can't choke on it.
    assert "&lt;raw&gt;" in msg
    assert "&amp;" in msg
    assert "<raw>" not in msg
