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
