"""Real-Chrome integration test for linkedin_scout.browser's extraction JS.

No network, no LinkedIn — local HTML fixtures with real open shadow DOM,
loaded via `file://` in a real Playwright + real Chrome (channel="chrome")
session. This is the one piece of M2 that a plain unit test (mocking the
Playwright API) cannot actually verify: whether the JS in
`browser._EXTRACT_JS` behaves as intended against a real browser's shadow DOM
implementation.

This test exists because the ORIGINAL version of `_EXTRACT_JS` (based on the
plan's claim that `document.body.innerText` already renders shadow DOM text)
was verified FALSE against real Chrome (2026-07-07): shadow content was
invisible to `innerText`, and the first fallback attempt used
`shadowRoot.textContent`, which silently drops all line breaks — breaking
`parser.parse_posts()`'s "Feed post"-marker line splitting. The current
`_EXTRACT_JS` fixes both. This test pins that fix with real fixtures modeling
the shapes described in docs/LINKEDIN_POSTS_SOURCE_PLAN.md §4.6.

Skipped automatically if real Chrome isn't installed (e.g. a CI image without
it) — this is a nice-to-have regression guard, not a hard requirement for the
rest of the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from linkedin_scout import browser
from linkedin_scout.heuristics import is_hiring_post
from linkedin_scout.parser import parse_posts

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "linkedin_scout" / "shadow_dom"

try:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as _pw:
        _browser = _pw.chromium.launch(channel="chrome", headless=True)
        _browser.close()
    _CHROME_AVAILABLE = True
except Exception:  # noqa: BLE001 — any failure means "skip this file"
    _CHROME_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _CHROME_AVAILABLE, reason="real Chrome (channel='chrome') not available"
)


def _extract(fixture_name: str) -> str:
    fixture_path = FIXTURES_DIR / fixture_name
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel="chrome", headless=True)
        try:
            page = b.new_page()
            page.goto(fixture_path.as_uri())
            return page.evaluate(browser._EXTRACT_JS) or ""
        finally:
            b.close()


def test_extract_js_reads_nested_shadow_dom_two_posts():
    text = _extract("two_posts_nested_shadow.html")
    posts = parse_posts(text)
    assert len(posts) == 2
    authors = {p.author for p in posts}
    assert authors == {"Deloitte Poland", "John Smith"}


def test_extract_js_output_is_hiring_filterable():
    text = _extract("two_posts_nested_shadow.html")
    posts = parse_posts(text)
    hiring = {p.author for p in posts if is_hiring_post(p.body)}
    # Deloitte post is a genuine remote Angular hiring post; the John Smith
    # post is US-staffing noise (W2, on-site Richmond VA) and must be rejected.
    assert hiring == {"Deloitte Poland"}


def test_extract_js_preserves_light_dom_text_next_to_shadow_sibling():
    text = _extract("mixed_light_and_shadow_siblings.html")
    assert "label text" in text
    assert "trailing text" in text
    posts = parse_posts(text)
    assert len(posts) == 1
    assert posts[0].author == "Mixed Author"


def test_extract_js_preserves_line_breaks_not_just_flat_textcontent():
    """Regression pin: a prior version used shadowRoot.textContent, which
    concatenates block-level siblings onto ONE line with no separator,
    making the "Feed post" marker unparseable. This must never happen again.
    """
    text = _extract("two_posts_nested_shadow.html")
    lines = [ln for ln in text.split("\n") if ln.strip()]
    assert "Feed post" in lines
    # If line breaks were lost, "Feed post" would be glued to the author name
    # on a single line instead of being its own line.
    assert not any(ln.startswith("Feed postDeloitte") for ln in lines)


def test_open_scroll_extract_actually_triggers_lazy_loaded_content(tmp_path):
    """Regression pin (2026-07-07, found during the owner's live verification):
    page.mouse.wheel() is silently a no-op unless page.mouse.move() was called
    at least once first — the owner's first two live runs against real
    LinkedIn showed an identical posts-visible count before and after
    scrolling. `infinite_scroll.html` only reveals its 2nd/3rd post in
    response to a real browser 'scroll' event, so this exercises the FULL
    `_open_scroll_extract` pipeline (not just a single `page.evaluate`) end to
    end and would fail again if the mouse.move() fix ever regresses.
    """
    fixture_path = FIXTURES_DIR / "infinite_scroll.html"
    text = browser._open_scroll_extract(
        fixture_path.as_uri(),
        profile_dir=tmp_path / "profile",
        storage_state_path=None,
        headless=True,
        scroll_iterations=3,
    )
    posts = parse_posts(text)
    assert len(posts) == 3
    authors = {p.author for p in posts}
    assert authors == {"Author Number 1", "Author Number 2", "Author Number 3"}


def test_open_scroll_extract_plateau_stops_early(tmp_path):
    """The feed track's plateau_limit must stop scrolling once the page
    genuinely runs out of new content, rather than burning through the full
    (up to ~10 minute) scroll_iterations/max_duration_sec budget for nothing.
    `infinite_scroll.html` caps out at 3 posts, so with a high scroll_iterations
    ceiling and a plateau_limit of 2, the loop must exit long before it would
    time out on its own — this is what the real feed track's ~200-iteration
    safety ceiling relies on to avoid a full 10-minute session every run.
    """
    import time

    fixture_path = FIXTURES_DIR / "infinite_scroll.html"
    start = time.monotonic()
    text = browser._open_scroll_extract(
        fixture_path.as_uri(),
        profile_dir=tmp_path / "profile",
        storage_state_path=None,
        headless=True,
        scroll_iterations=200,
        scroll_wait_range=(0.05, 0.1),
        plateau_limit=2,
    )
    elapsed = time.monotonic() - start
    posts = parse_posts(text)
    assert len(posts) == 3
    # 200 iterations at even the fastest wait range would take >10s; plateau
    # detection must cut it off after a handful of scrolls instead.
    assert elapsed < 5
