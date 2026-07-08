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
    seed_profile_cookies,
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


# --- seed_profile_cookies (fake context, no Playwright) ----------------------
#
# Design note (2026-07-07): cookies are re-seeded on EVERY call, not once —
# verified empirically against a real Chrome persistent context that
# Playwright's add_cookies() does NOT survive to the next process launch (see
# browser.seed_profile_cookies docstring). These tests reflect that: there is
# no "already seeded, skip" branch to test, only "seed every time there's
# something to seed".


class _FakeContext:
    def __init__(self) -> None:
        self.add_cookies_calls: list[list[dict]] = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.add_cookies_calls.append(cookies)


def test_seed_profile_cookies_seeds_from_storage_state(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text(
        json.dumps({"cookies": [{"name": "li_at", "value": "abc"}], "origins": []}),
        encoding="utf-8",
    )
    ctx = _FakeContext()

    result = seed_profile_cookies(ctx, storage_state_path)

    assert result is True
    assert ctx.add_cookies_calls == [[{"name": "li_at", "value": "abc"}]]


def test_seed_profile_cookies_seeds_every_call(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text(
        json.dumps({"cookies": [{"name": "li_at", "value": "abc"}]}), encoding="utf-8"
    )
    ctx = _FakeContext()

    seed_profile_cookies(ctx, storage_state_path)
    seed_profile_cookies(ctx, storage_state_path)

    assert len(ctx.add_cookies_calls) == 2


def test_seed_profile_cookies_no_storage_state_path():
    ctx = _FakeContext()

    result = seed_profile_cookies(ctx, None)

    assert result is False
    assert ctx.add_cookies_calls == []


def test_seed_profile_cookies_missing_storage_state_file(tmp_path):
    ctx = _FakeContext()

    result = seed_profile_cookies(ctx, tmp_path / "does_not_exist.json")

    assert result is False
    assert ctx.add_cookies_calls == []


def test_seed_profile_cookies_empty_cookie_list(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")
    ctx = _FakeContext()

    result = seed_profile_cookies(ctx, storage_state_path)

    assert result is False
    assert ctx.add_cookies_calls == []


def test_seed_profile_cookies_corrupt_storage_state_is_best_effort(tmp_path):
    storage_state_path = tmp_path / "storage_state.json"
    storage_state_path.write_text("{ not valid json", encoding="utf-8")
    ctx = _FakeContext()

    result = seed_profile_cookies(ctx, storage_state_path)

    assert result is False
    assert ctx.add_cookies_calls == []


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


def test_run_once_threads_permalink_through_to_candidate(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    raw_text = (
        "Feed post\n\nDeloitte Poland\n3rd+\nTalent Acquisition\n2h\nFollow\n"
        "LI_PERMALINK::https://www.linkedin.com/feed/update/urn:li:share:999/\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
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
    assert result[0].permalink == "https://www.linkedin.com/feed/update/urn:li:share:999/"


def test_run_once_no_permalink_marker_leaves_candidate_permalink_none(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    raw_text = (
        "Feed post\n\nDeloitte Poland\n3rd+\nTalent Acquisition\n2h\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
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
    assert result[0].permalink is None


def test_run_once_searches_every_keyword_in_one_call(tmp_path, monkeypatch):
    """Owner decision (2026-07-08): one run_once() call now searches the
    ENTIRE keyword list, not one rotation-keyword per call. Order is
    randomized (see next test), so only the SET is asserted here."""
    state = ScoutState(tmp_path / "state.json")
    monkeypatch.setattr(browser, "_sleep_human", lambda *a, **k: None)

    seen_keywords = []
    monkeypatch.setattr(
        browser, "scout_keyword", lambda keyword, **k: seen_keywords.append(keyword) or ""
    )

    run_once(["a", "b", "c"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)

    assert sorted(seen_keywords) == ["a", "b", "c"]
    assert len(seen_keywords) == 3  # each keyword searched exactly once


def test_run_once_shuffles_keyword_order(tmp_path, monkeypatch):
    """Owner decision (2026-07-08): keyword order must be freshly randomized
    each call, not the same fixed round-robin sequence every time."""
    state = ScoutState(tmp_path / "state.json")
    monkeypatch.setattr(browser, "_sleep_human", lambda *a, **k: None)
    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: "")

    shuffle_calls = []
    original_shuffle = browser.random.shuffle
    monkeypatch.setattr(
        browser.random,
        "shuffle",
        lambda seq: (shuffle_calls.append(list(seq)), original_shuffle(seq))[1],
    )

    run_once(["a", "b", "c"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)

    assert len(shuffle_calls) == 1  # random.shuffle() was actually invoked


def test_run_once_sleeps_between_keywords_not_after_last(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    monkeypatch.setattr(browser, "scout_keyword", lambda *a, **k: "")

    sleep_calls = []
    monkeypatch.setattr(browser, "_sleep_human", lambda range_sec: sleep_calls.append(range_sec))

    run_once(["a", "b", "c"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)

    # 3 keywords -> 2 between-keyword pauses, none after the last one
    assert len(sleep_calls) == 2
    assert all(rng == browser._BETWEEN_KEYWORD_WAIT_RANGE_SEC for rng in sleep_calls)


def test_run_once_stops_remaining_keywords_after_trip(tmp_path, monkeypatch):
    state = ScoutState(tmp_path / "state.json")
    monkeypatch.setattr(browser, "_sleep_human", lambda *a, **k: None)
    monkeypatch.setattr(browser, "_send_circuit_breaker_alert", lambda reason: None)

    attempted = []

    def _scout(keyword, **k):
        attempted.append(keyword)
        raise AntiBotDetected("flagged")

    monkeypatch.setattr(browser, "scout_keyword", _scout)

    result = run_once(["a", "b", "c"], profile_dir=tmp_path / "profile", storage_state_path=None, state=state)

    assert result == []
    # tripped on whichever keyword came first in the randomized order —
    # exactly one attempt, the other two never tried.
    assert len(attempted) == 1
    assert attempted[0] in ("a", "b", "c")
    assert state.is_tripped() is True


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


# --- off-screen window positioning (search track only) -----------------------


class _FakeChromium:
    def __init__(self) -> None:
        self.launch_kwargs: dict | None = None

    def launch_persistent_context(self, **kwargs):
        self.launch_kwargs = kwargs

        class _FakeContext:
            def add_cookies(self, cookies):
                pass

            def add_init_script(self, script):
                pass

            def new_page(self):
                raise AntiBotDetected("stop before any real navigation")

            def close(self):
                pass

        return _FakeContext()


class _FakePlaywright:
    def __init__(self, chromium: _FakeChromium) -> None:
        self.chromium = chromium

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_scout_keyword_launches_offscreen(tmp_path, monkeypatch):
    fake_chromium = _FakeChromium()
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: _FakePlaywright(fake_chromium)
    )

    try:
        browser.scout_keyword(
            "angular", profile_dir=tmp_path / "profile", storage_state_path=None
        )
    except AntiBotDetected:
        pass  # expected — the fake context stops right after launch

    assert fake_chromium.launch_kwargs is not None
    assert "--window-position=-3000,0" in fake_chromium.launch_kwargs["args"]


def test_scout_feed_does_not_launch_offscreen(tmp_path, monkeypatch):
    fake_chromium = _FakeChromium()
    monkeypatch.setattr(
        "playwright.sync_api.sync_playwright", lambda: _FakePlaywright(fake_chromium)
    )

    try:
        browser.scout_feed(profile_dir=tmp_path / "profile", storage_state_path=None)
    except AntiBotDetected:
        pass

    assert fake_chromium.launch_kwargs is not None
    assert "--window-position=-3000,0" not in fake_chromium.launch_kwargs["args"]


# --- menu-click permalink capture (owner discovery 2026-07-08) --------------


def test_filter_candidates_backfills_permalink_from_menu_dict():
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
    )
    key = browser.dedup_key(
        "Deloitte Poland", "We're hiring an Angular Developer. Fully remote across Poland."
    )
    menu_permalinks = {key: "https://www.linkedin.com/feed/update/urn:li:activity:1/"}

    candidates = browser._filter_candidates(raw_text, "angular", menu_permalinks)

    assert len(candidates) == 1
    assert candidates[0].permalink == "https://www.linkedin.com/feed/update/urn:li:activity:1/"


def test_filter_candidates_dom_marker_permalink_wins_over_menu_dict():
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "LI_PERMALINK::https://www.linkedin.com/feed/update/urn:li:share:999/\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
    )
    key = browser.dedup_key(
        "Deloitte Poland", "We're hiring an Angular Developer. Fully remote across Poland."
    )
    menu_permalinks = {key: "https://www.linkedin.com/feed/update/urn:li:activity:should-not-win/"}

    candidates = browser._filter_candidates(raw_text, "angular", menu_permalinks)

    assert candidates[0].permalink == "https://www.linkedin.com/feed/update/urn:li:share:999/"


def test_filter_candidates_no_menu_dict_entry_leaves_permalink_none():
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
    )
    candidates = browser._filter_candidates(raw_text, "angular", {})
    assert candidates[0].permalink is None


class _FakeLocator:
    """Minimal stand-in for a Playwright Locator: chains `.filter()`/`.locator()`,
    exposes `.first` (self, for chaining) and a fixed `.count()`. Click
    behavior is injected via a callback so tests can assert click order and
    simulate failures without a real browser."""

    def __init__(self, count: int = 0, on_click=None, children: dict[str, "_FakeLocator"] | None = None):
        self._count = count
        self._on_click = on_click
        self._children = children or {}

    @property
    def first(self):
        return self

    def filter(self, has_text=None):
        return self

    def locator(self, selector: str):
        return self._children.get(selector, _FakeLocator(count=0))

    def count(self):
        return self._count

    def click(self, timeout=None):
        if self._on_click:
            self._on_click()


class _FakePage:
    def __init__(self, container: _FakeLocator, copy_item: _FakeLocator, clipboard_text: str = ""):
        self._container = container
        self._copy_item = copy_item
        self._clipboard_text = clipboard_text
        self.evaluate_calls: list[str] = []
        self.escape_presses = 0
        self.keyboard = self

    def locator(self, selector: str):
        return self._container if selector == browser._POST_CONTAINER_SELECTORS[0] else _FakeLocator(count=0)

    def get_by_text(self, text: str, exact: bool = False):
        return self._copy_item

    def evaluate(self, js: str):
        self.evaluate_calls.append(js)
        return self._clipboard_text

    def press(self, key: str):
        if key == "Escape":
            self.escape_presses += 1


def test_copy_link_via_menu_happy_path():
    button = _FakeLocator(count=1)
    container = _FakeLocator(count=1, children={browser._MENU_BUTTON_SELECTORS[0]: button})
    copy_item = _FakeLocator(count=1)
    page = _FakePage(
        container, copy_item, clipboard_text="https://www.linkedin.com/feed/update/urn:li:activity:1/"
    )

    link = browser._copy_link_via_menu(page, "Deloitte Poland", "We're hiring an Angular Developer.")

    assert link == "https://www.linkedin.com/feed/update/urn:li:activity:1/"


def test_copy_link_via_menu_no_container_returns_none():
    page = _FakePage(_FakeLocator(count=0), _FakeLocator(count=0))
    assert browser._copy_link_via_menu(page, "Deloitte Poland", "We're hiring an Angular Developer.") is None


def test_copy_link_via_menu_clipboard_not_linkedin_returns_none():
    button = _FakeLocator(count=1)
    container = _FakeLocator(count=1, children={browser._MENU_BUTTON_SELECTORS[0]: button})
    copy_item = _FakeLocator(count=1)
    page = _FakePage(container, copy_item, clipboard_text="not a link")

    link = browser._copy_link_via_menu(page, "Deloitte Poland", "We're hiring an Angular Developer.")

    assert link is None


def test_fetch_menu_permalinks_skips_posts_with_existing_marker():
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "LI_PERMALINK::https://www.linkedin.com/feed/update/urn:li:share:1/\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
    )
    page = _FakePage(_FakeLocator(count=1), _FakeLocator(count=1), clipboard_text="")
    result = browser._fetch_menu_permalinks(page, raw_text)
    assert result == {}


def test_fetch_menu_permalinks_skips_non_hiring_posts():
    raw_text = "Feed post\n\nJohn Doe\nFollow\nJust a random update about my weekend.\n"
    page = _FakePage(_FakeLocator(count=1), _FakeLocator(count=1), clipboard_text="")
    result = browser._fetch_menu_permalinks(page, raw_text)
    assert result == {}


def test_fetch_menu_permalinks_captures_for_hiring_candidate():
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "We're hiring an Angular Developer. Fully remote across Poland.\n"
    )
    button = _FakeLocator(count=1)
    container = _FakeLocator(count=1, children={browser._MENU_BUTTON_SELECTORS[0]: button})
    copy_item = _FakeLocator(count=1)
    page = _FakePage(
        container, copy_item, clipboard_text="https://www.linkedin.com/feed/update/urn:li:activity:1/"
    )

    result = browser._fetch_menu_permalinks(page, raw_text)

    key = browser.dedup_key(
        "Deloitte Poland", "We're hiring an Angular Developer. Fully remote across Poland."
    )
    assert result == {key: "https://www.linkedin.com/feed/update/urn:li:activity:1/"}


def test_fetch_menu_permalinks_caps_at_max_attempts(monkeypatch):
    monkeypatch.setattr(browser, "_MAX_MENU_PERMALINK_ATTEMPTS", 1)
    raw_text = (
        "Feed post\n\nDeloitte Poland\nFollow\n"
        "We're hiring an Angular Developer #1. Fully remote across Poland.\n"
        "\nFeed post\n\nAcme Corp\nFollow\n"
        "We're hiring an Angular Developer #2. Fully remote across Poland.\n"
    )
    calls = []
    monkeypatch.setattr(browser, "_copy_link_via_menu", lambda page, author, body: calls.append(author) or None)
    result = browser._fetch_menu_permalinks(_FakePage(_FakeLocator(), _FakeLocator()), raw_text)

    assert len(calls) == 1
    assert result == {}
