"""Tests for linkedin_scout.telegram_relay — the scout's Telegram-based
handoff to the bot (replaces the old local-queue-file design once it became
clear the bot and the scout don't share a filesystem).
"""

from __future__ import annotations

import base64
import json

import linkedin_scout.telegram_relay as telegram_relay
from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.seen_store import SeenStore, dedup_key
from linkedin_scout.telegram_relay import build_payload, send_candidates


def _candidate(**overrides) -> ScoutCandidate:
    defaults = dict(
        keyword="angular hiring",
        author="Deloitte Poland",
        body="We're hiring an Angular Developer. Fully remote.",
        scouted_at="2026-07-08T12:00:00+00:00",
        permalink="https://www.linkedin.com/feed/update/urn:li:share:1/",
    )
    defaults.update(overrides)
    return ScoutCandidate(**defaults)


# --- build_payload -------------------------------------------------------


def test_build_payload_roundtrips():
    candidate = _candidate()
    payload = build_payload(candidate)
    decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    assert decoded["author"] == "Deloitte Poland"
    assert decoded["keyword"] == "angular hiring"
    assert "hiring" in decoded["body"].lower()


def test_build_payload_truncates_long_body():
    long_body = "We're hiring an Angular Developer. " * 200  # > 3000 chars
    candidate = _candidate(body=long_body)
    payload = build_payload(candidate)
    decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    assert len(decoded["body"]) <= telegram_relay._MAX_BODY_CHARS


def test_build_payload_is_url_safe_base64_ascii():
    candidate = _candidate(body="Some post with unicode: Wrocław, zdalnie, 100%")
    payload = build_payload(candidate)
    # must be plain ASCII (safe as a single Telegram command argument)
    payload.encode("ascii")


def test_build_payload_includes_permalink_when_present():
    candidate = _candidate(permalink="https://www.linkedin.com/feed/update/urn:li:share:1/")
    payload = build_payload(candidate)
    decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    assert decoded["permalink"] == "https://www.linkedin.com/feed/update/urn:li:share:1/"


def test_build_payload_permalink_none_when_absent():
    candidate = _candidate(permalink=None)
    payload = build_payload(candidate)
    decoded = json.loads(base64.b64decode(payload).decode("utf-8"))
    assert decoded["permalink"] is None


# --- send_candidates ------------------------------------------------------


def test_send_candidates_noop_without_config(tmp_path, monkeypatch):
    for var in (
        "TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_BOT_USERNAME", "TELEGRAM_USER_SESSION",
    ):
        monkeypatch.delenv(var, raising=False)

    seen_store = SeenStore(tmp_path / "seen.json")
    sent = send_candidates([_candidate()], seen_store)

    assert sent == 0


def test_send_candidates_noop_without_session_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@mybot")
    monkeypatch.setenv("TELEGRAM_USER_SESSION", str(tmp_path / "does_not_exist"))

    seen_store = SeenStore(tmp_path / "seen.json")
    sent = send_candidates([_candidate()], seen_store)

    assert sent == 0


class _FakeTelethonClient:
    sent_messages: list[tuple[str, str]] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send_message(self, target, text):
        _FakeTelethonClient.sent_messages.append((target, text))


def test_send_candidates_sends_new_and_marks_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@mybot")
    session_path = tmp_path / "session"
    (tmp_path / "session.session").write_text("", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_USER_SESSION", str(session_path))

    _FakeTelethonClient.sent_messages = []
    import telethon.sync as telethon_sync_module

    monkeypatch.setattr(telethon_sync_module, "TelegramClient", _FakeTelethonClient)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate()
    sent = send_candidates([candidate], seen_store)

    assert sent == 1
    assert len(_FakeTelethonClient.sent_messages) == 1
    target, text = _FakeTelethonClient.sent_messages[0]
    assert target == "@mybot"
    assert text.startswith("/scoutfound ")

    key = dedup_key(candidate.author, candidate.body)
    reloaded = SeenStore(tmp_path / "seen.json")
    assert reloaded.is_seen(key) is True


def test_send_candidates_skips_already_seen(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@mybot")
    session_path = tmp_path / "session"
    (tmp_path / "session.session").write_text("", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_USER_SESSION", str(session_path))

    _FakeTelethonClient.sent_messages = []
    import telethon.sync as telethon_sync_module

    monkeypatch.setattr(telethon_sync_module, "TelegramClient", _FakeTelethonClient)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate()
    seen_store.mark_seen(dedup_key(candidate.author, candidate.body))
    seen_store.save()

    sent = send_candidates([candidate], seen_store)

    assert sent == 0
    assert _FakeTelethonClient.sent_messages == []


def test_send_candidates_skips_candidate_without_permalink(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_API_ID", "123")
    monkeypatch.setenv("TELEGRAM_API_HASH", "abc")
    monkeypatch.setenv("TELEGRAM_BOT_USERNAME", "@mybot")
    session_path = tmp_path / "session"
    (tmp_path / "session.session").write_text("", encoding="utf-8")
    monkeypatch.setenv("TELEGRAM_USER_SESSION", str(session_path))

    _FakeTelethonClient.sent_messages = []
    import telethon.sync as telethon_sync_module

    monkeypatch.setattr(telethon_sync_module, "TelegramClient", _FakeTelethonClient)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate(permalink=None)

    sent = send_candidates([candidate], seen_store)

    assert sent == 0
    assert _FakeTelethonClient.sent_messages == []
    # not marked seen — a future run (better DOM luck / fixed selectors)
    # must get another shot at this same post
    key = dedup_key(candidate.author, candidate.body)
    assert seen_store.is_seen(key) is False
