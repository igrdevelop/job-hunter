"""Tests for the unit-testable pieces of linkedin_scout.browser.

No real Playwright browser is launched here — scout_keyword() itself (the
actual page automation) is exercised only by a live run on the owner's
machine, per the task spec. These tests cover everything that doesn't need a
live LinkedIn session: URL/anti-bot detection, profile-seeding logic (against
a fake context object), the Telegram alert helper, and run_once()'s circuit-
breaker + M1-filter wiring (with scout_keyword monkeypatched).
"""

from __future__ import annotations

import json

import linkedin_scout.browser as browser
from linkedin_scout.browser import (
    FEED_URL,
    AntiBotDetected,
    ScoutCandidate,
    build_search_url,
    is_blocked_url,
    looks_like_anti_bot,
    run_feed_once,
    run_once,
    seed_profile_if_needed,
)
from linkedin_scout.state import ScoutState


# --- is_blocked_url / looks_like_anti_bot ------------------------------------


def test_is_blocked_url_login_redirect():
    assert is_blocked_url("https://www.linkedin.com/login") is True


def test_is_blocked_url_checkpoint_redirect():
    assert is_blocked_url("https://www.linkedin.com/checkpoint/challenge") is True


def test_is_blocked_url_authwall_redirect():
    assert is_blocked_url("https://www.linkedin.com/authwall?trk=x") is True


def test_is_blocked_url_normal_search_page():
    url = "https://www.linkedin.com/search/results/content/?keywords=angular"
    assert is_blocked_url(url) is False


def test_looks_like_anti_bot_captcha_marker():
    assert looks_like_anti_bot("Please complete this CAPTCHA to continue") is True


def test_looks_like_anti_bot_protechts_marker():
    assert looks_like_anti_bot("blocked by li.protechts.net uc=scraping") is True


def test_looks_like_anti_bot_normal_text():
    assert looks_like_anti_bot("We're hiring an Angular developer") is False


def test_looks_like_anti_bot_empty_text():
    assert looks_like_anti_bot("") is False


# --- build_search_url ---------------------------------------------------------


def test_build_search_url_encodes_keyword():
    url = build_search_url("angular praca zdalna")
    assert "angular%20praca%20zdalna" in url
    assert "sortBy=%22date_posted%22" in url
    assert "datePosted=%22past-week%22" in url


# --- seed_profile_if_needed (fake context, no Playwright) -------------------


class _FakeContext:
    def __init__(self) -> None:
        self.added_cookies: list[dict] | None = None

    def add_cookies(self, cookies: list[dict]) -> None:
        self.added_cookies = cookies


def test_seed_profile_seeds_cookies_once(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text(
        json.dumps({"cookies": [{"name": "li_at", "value": "abc"}], "origins": []}),
        encoding="utf-8",
    )
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = _FakeContext()

    result = seed_profile_if_needed(ctx, profile_dir, storage_state_path)

    assert result is True
    assert ctx.added_cookies == [{"name": "li_at", "value": "abc"}]
    assert (profile_dir / ".seeded").exists()


def test_seed_profile_skips_when_already_seeded(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text(json.dumps({"cookies": [{"name": "li_at"}]}), encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / ".seeded").write_text("already", encoding="utf-8")
    ctx = _FakeContext()

    result = seed_profile_if_needed(ctx, profile_dir, storage_state_path)

    assert result is False
    assert ctx.added_cookies is None


def test_seed_profile_no_storage_state_path(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = _FakeContext()

    result = seed_profile_if_needed(ctx, profile_dir, None)

    assert result is False
    assert ctx.added_cookies is None
    assert not (profile_dir / ".seeded").exists()


def test_seed_profile_missing_storage_state_file(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = _FakeContext()

    result = seed_profile_if_needed(ctx, profile_dir, tmp_path / "does_not_exist.json")

    assert result is False
    assert ctx.added_cookies is None


def test_seed_profile_corrupt_storage_state_marks_seeded_anyway(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text("{ not valid json", encoding="utf-8")
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    ctx = _FakeContext()

    result = seed_profile_if_needed(ctx, profile_dir, storage_state_path)

    assert result is False
    assert ctx.added_cookies is None
    # Marked seeded even on failure so a permanently-malformed file doesn't
    # retry (and log-spam) on every single invocation.
    assert (profile_dir / ".seeded").exists()


# --- _send_circuit_breaker_alert ---------------------------------------------


def test_send_alert_noop_without_telegram_config(monkeypatch):
    monkeypatch.setattr(browser, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(browser, "TELEGRAM_CHAT_ID", 0)
    assert browser._send_circuit_breaker_alert("reason") is False


def test_send_alert_posts_to_telegram(monkeypatch):
    monkeypatch.setattr(browser, "TELEGRAM_BOT_TOKEN", "token123")
    monkeypatch.setattr(browser, "TELEGRAM_CHAT_ID", 42)

    calls = []

    class _FakeResp:
        ok = True

    def _fake_post(url, json, timeout):  # noqa: A002
        calls.append((url, json, timeout))
        return _FakeResp()

    import requests

    monkeypatch.setattr(requests, "post", _fake_post)

    assert browser._send_circuit_breaker_alert("session flagged") is True
    assert len(calls) == 1
    url, payload, _timeout = calls[0]
    assert "token123" in url
    assert payload["chat_id"] == 42
    assert "session flagged" in payload["text"]
    assert "--reset" in payload["text"]


# --- run_once -----------------------------------------------------------------


def test_run_once_noop_when_tripped(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    state.trip("already flagged")

    called = []
    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: called.append(1))

    result = run_once(
        ["angular hiring"],
        profile_dir=tmp_path / "profile",
        storage_state_path=None,
        state=state,
    )

    assert result == []
    assert called == []


def test_run_once_trips_and_alerts_exactly_once_on_anti_bot(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")

    def _raise(*args, **kwargs):
        raise AntiBotDetected("redirected to login")

    monkeypatch.setattr(browser, "scout_keyword", _raise)
    alerts = []
    monkeypatch.setattr(browser, "_send_circuit_breaker_alert", lambda reason: alerts.append(reason))

    result = run_once(
        ["angular hiring"],
        profile_dir=tmp_path / "profile",
        storage_state_path=None,
        state=state,
    )

    assert result == []
    assert state.is_tripped() is True
    assert alerts == ["redirected to login"]


def test_run_once_filters_through_m1_heuristic_and_location_gate(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    raw_text = (
        "Feed post\n\nDeloitte Poland\n3rd+\nTalent Acquisition\n2h\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
        "Like\nComment\nShare\n"
        "\nFeed post\n\nJohn Smith\n2nd\nRecruiter\n5h\nConnect\n"
        "Szukam nowego projektu jako Angular Developer, zdalnie.\n"
        "Like\nComment\nShare\n"
    )
    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: raw_text)

    result = run_once(
        ["angular hiring"],
        profile_dir=tmp_path / "profile",
        storage_state_path=None,
        state=state,
    )

    assert len(result) == 1
    candidate = result[0]
    assert isinstance(candidate, ScoutCandidate)
    assert candidate.author == "Deloitte Poland"
    assert candidate.keyword == "angular hiring"
    assert "We're hiring" in candidate.body


def test_run_once_advances_keyword_rotation(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: "")

    run_once(["a", "b"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)
    run_once(["a", "b"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)

    # Third call should be back to "a" — round robin advanced correctly.
    seen_keywords = []
    monkeypatch.setattr(
        browser, "scout_keyword", lambda keyword, **k: seen_keywords.append(keyword) or ""
    )
    run_once(["a", "b"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)
    assert seen_keywords == ["a"]


# --- feed track (scout_feed / run_feed_once) ---------------------------------


def test_feed_url_has_no_keyword_param():
    assert FEED_URL == "https://www.linkedin.com/feed/"
    assert "keywords" not in FEED_URL


def test_run_feed_once_noop_when_tripped(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "feed_state.json")
    state.trip("already flagged")

    called = []
    monkeypatch.setattr(browser, "scout_feed", lambda *a, **k: called.append(1))

    result = run_feed_once(
        profile_dir=tmp_path / "feed_profile",
        storage_state_path=None,
        state=state,
    )

    assert result == []
    assert called == []


def test_run_feed_once_trips_and_alerts_exactly_once_on_anti_bot(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "feed_state.json")

    def _raise(*args, **kwargs):
        raise AntiBotDetected("redirected to checkpoint")

    monkeypatch.setattr(browser, "scout_feed", _raise)
    alerts = []
    monkeypatch.setattr(browser, "_send_circuit_breaker_alert", lambda reason: alerts.append(reason))

    result = run_feed_once(
        profile_dir=tmp_path / "feed_profile",
        storage_state_path=None,
        state=state,
    )

    assert result == []
    assert state.is_tripped() is True
    assert alerts == ["redirected to checkpoint"]


def test_run_feed_once_filters_through_m1_and_labels_candidates_feed(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "feed_state.json")
    raw_text = (
        "Feed post\n\nDeloitte Poland\n3rd+\nTalent Acquisition\n2h\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
        "Like\nComment\nShare\n"
    )
    monkeypatch.setattr(browser, "scout_feed", lambda *a, **k: raw_text)

    result = run_feed_once(
        profile_dir=tmp_path / "feed_profile",
        storage_state_path=None,
        state=state,
    )

    assert len(result) == 1
    candidate = result[0]
    assert isinstance(candidate, ScoutCandidate)
    assert candidate.author == "Deloitte Poland"
    assert candidate.keyword == "feed"


def test_feed_and_keyword_tracks_use_independent_state(tmp_path, monkeypatch):
    """A trip on the feed track must not silence the keyword-search track."""
    feed_state = ScoutState(tmp_path / "feed_state.json")
    search_state = ScoutState(tmp_path / "search_state.json")

    def _raise(*args, **kwargs):
        raise AntiBotDetected("flagged")

    monkeypatch.setattr(browser, "scout_feed", _raise)
    run_feed_once(profile_dir=tmp_path / "feed_profile", storage_state_path=None, state=feed_state)

    assert feed_state.is_tripped() is True
    assert search_state.is_tripped() is False

    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: "")
    result = run_once(
        ["angular"],
        profile_dir=tmp_path / "search_profile",
        storage_state_path=None,
        state=search_state,
    )
    assert result == []  # empty because raw_text is empty, not because tripped
