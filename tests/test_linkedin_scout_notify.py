"""Tests for linkedin_scout.notify — message formatting + Telegram delivery.

No network calls: requests.post is monkeypatched.
"""

from __future__ import annotations

import linkedin_scout.notify as notify
from linkedin_scout.browser import ScoutCandidate
from linkedin_scout.notify import format_message, notify_candidates
from linkedin_scout.seen_store import SeenStore, dedup_key


def _candidate(**overrides) -> ScoutCandidate:
    defaults = {
        "keyword": "angular hiring",
        "author": "Deloitte Poland",
        "body": "We're hiring an Angular Developer. Fully remote across Poland.",
        "scouted_at": "2026-07-07T12:00:00+00:00",
    }
    defaults.update(overrides)
    return ScoutCandidate(**defaults)


# --- format_message -----------------------------------------------------------


def test_format_message_includes_author_keyword_snippet_timestamp():
    text = format_message(_candidate())
    assert "angular hiring" in text
    assert "Deloitte Poland" in text
    assert "We're hiring an Angular Developer" in text
    assert "2026-07-07T12:00:00+00:00" in text


def test_format_message_omits_profile_link_when_absent():
    text = format_message(_candidate(author_profile_url=None))
    assert "linkedin.com/in/" not in text


def test_format_message_includes_profile_link_when_present():
    text = format_message(_candidate(author_profile_url="https://www.linkedin.com/in/janedoe"))
    assert "https://www.linkedin.com/in/janedoe" in text


def test_format_message_truncates_long_body_to_300_chars_with_ellipsis():
    long_body = "We're hiring an Angular Developer. " * 20  # > 300 chars
    text = format_message(_candidate(body=long_body))
    assert "…" in text
    snippet_line = [ln for ln in text.split("\n") if ln.startswith("We're hiring")][0]
    assert len(snippet_line) <= 301  # 300 chars + ellipsis


def test_format_message_short_body_not_truncated():
    short_body = "We're hiring an Angular Developer."
    text = format_message(_candidate(body=short_body))
    assert "…" not in text
    assert short_body in text


# --- _send_telegram ------------------------------------------------------------


def test_send_telegram_noop_without_config(monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(notify, "TELEGRAM_CHAT_ID", 0)
    assert notify._send_telegram("hello") is False


def test_send_telegram_posts_to_api(monkeypatch):
    monkeypatch.setattr(notify, "TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setattr(notify, "TELEGRAM_CHAT_ID", 99)

    calls = []

    class _FakeResp:
        ok = True

    def _fake_post(url, json, timeout):  # noqa: A002
        calls.append((url, json))
        return _FakeResp()

    import requests

    monkeypatch.setattr(requests, "post", _fake_post)

    assert notify._send_telegram("hello world") is True
    assert len(calls) == 1
    url, payload = calls[0]
    assert "tok" in url
    assert payload["chat_id"] == 99
    assert payload["text"] == "hello world"


# --- notify_candidates ---------------------------------------------------------


def test_notify_candidates_sends_new_and_marks_seen(tmp_path, monkeypatch):
    sent_texts = []
    monkeypatch.setattr(notify, "_send_telegram", lambda text: sent_texts.append(text) or True)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate()

    sent = notify_candidates([candidate], seen_store)

    assert sent == 1
    assert len(sent_texts) == 1
    key = dedup_key(candidate.author, candidate.body)
    assert seen_store.is_seen(key) is True
    # persisted to disk, not just in memory
    reloaded = SeenStore(tmp_path / "seen.json")
    assert reloaded.is_seen(key) is True


def test_notify_candidates_skips_already_seen(tmp_path, monkeypatch):
    sent_texts = []
    monkeypatch.setattr(notify, "_send_telegram", lambda text: sent_texts.append(text) or True)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate()
    seen_store.mark_seen(dedup_key(candidate.author, candidate.body))
    seen_store.save()

    sent = notify_candidates([candidate], seen_store)

    assert sent == 0
    assert sent_texts == []


def test_notify_candidates_does_not_mark_seen_on_failed_send(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_send_telegram", lambda text: False)

    seen_store = SeenStore(tmp_path / "seen.json")
    candidate = _candidate()

    sent = notify_candidates([candidate], seen_store)

    assert sent == 0
    key = dedup_key(candidate.author, candidate.body)
    assert seen_store.is_seen(key) is False


def test_notify_candidates_mixed_batch_counts_only_new_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(notify, "_send_telegram", lambda text: True)

    seen_store = SeenStore(tmp_path / "seen.json")
    already_seen = _candidate(author="Old Author")
    seen_store.mark_seen(dedup_key(already_seen.author, already_seen.body))
    seen_store.save()

    new_one = _candidate(author="New Author")
    sent = notify_candidates([already_seen, new_one], seen_store)

    assert sent == 1
