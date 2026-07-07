"""Playwright scraping for the LinkedIn posts scout (M2).

Design per docs/LINKEDIN_POSTS_SCOUT_TASK.md §3.1/§3.5 and the live-probe
findings in docs/LINKEDIN_POSTS_SOURCE_PLAN.md §4.6 (branch
feat/linkedin-posts-source):

- Persistent Chrome profile (channel="chrome", headed), seeded once from
  LINKEDIN_STORAGE_STATE cookies.
- ONE page load per keyword per run, human-paced waits, no parallelism.
- Circuit breaker: any login/checkpoint/authwall redirect or anti-bot response
  aborts immediately (no retries) and trips the persisted state (state.py) so
  every subsequent run no-ops until the owner clears it with `--reset`.

Playwright itself is imported lazily inside the functions that need it, so
this module (and its pure helpers) can be imported and unit-tested in an
environment without a real browser session available.

IMPORTANT: the exact search-result selectors/behaviour below have NOT been
verified against a live LinkedIn session in this change — the live-probe
findings this module implements come from an earlier session (plan §4.6).
Per the task spec, M2 is reviewed on paper first; a live run on the owner's
machine is the actual verification step, and `docs/LINKEDIN_POSTS_SOURCE_PLAN.md`
already documents that this DOM is the most fragile of any scraper in the repo.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
from linkedin_scout.heuristics import LocationVerdict, check_location, is_hiring_post
from linkedin_scout.parser import ParsedPost, parse_posts

logger = logging.getLogger("linkedin_scout.browser")

SEARCH_URL_TEMPLATE = (
    "https://www.linkedin.com/search/results/content/"
    "?keywords={kw}&sortBy=%22date_posted%22&datePosted=%22past-week%22"
)

# The home feed itself — no keyword, no content-search surface. Owner-requested
# second track (2026-07-07): scroll the main feed for ANY post, filtered by the
# same is_hiring_post()/check_location() gate as the keyword search (that gate
# already requires "angular" to be prominent in the text, so this naturally
# narrows to Angular-relevant posts without needing a query param).
FEED_URL = "https://www.linkedin.com/feed/"

# Feed scroll goes deeper than a single search page — "all posts", not one
# page of results — so it gets its own (larger) scroll budget.
_FEED_SCROLL_ITERATIONS = 8

# Real installed Chrome (not bundled Chromium) + stealth flags — headless
# Chromium got flagged within 2-3 loads in the live probe (plan §4.6 #4).
STEALTH_CHROME_ARGS: tuple[str, ...] = (
    "--disable-blink-features=AutomationControlled",
)

_HIDE_WEBDRIVER_INIT_SCRIPT = (
    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
)

# Human-paced waits between actions (seconds) — no parallelism, no rapid-fire.
_SCROLL_ITERATIONS = 3
_SCROLL_WAIT_RANGE_SEC = (1.0, 2.0)
_POST_LOAD_WAIT_RANGE_SEC = (1.5, 2.5)

_SEEDED_MARKER_NAME = ".seeded"


class AntiBotDetected(Exception):
    """Raised the moment a login/checkpoint/authwall/captcha response is seen.

    Circuit-breaker rule (task spec §3.5): no retries, ever. The caller must
    trip the persisted state and abort — never try again in the same run.
    """


# --- Pure / unit-testable helpers --------------------------------------------


def is_blocked_url(url: str) -> bool:
    """True if `url` is LinkedIn's login/checkpoint/authwall redirect."""
    return any(
        marker in url
        for marker in ("linkedin.com/login", "linkedin.com/checkpoint", "linkedin.com/authwall")
    )


# Substrings seen on LinkedIn's anti-bot interstitials during the live probe
# (plan §4.6 #4: "li.protechts.net ... uc=scraping" + reCAPTCHA).
_ANTI_BOT_MARKERS: tuple[str, ...] = (
    "protechts.net",
    "recaptcha",
    "captcha",
    "verify you're a human",
    "verify you are a human",
    "unusual activity",
    "let's do a quick security check",
)


def looks_like_anti_bot(text: str) -> bool:
    """True if page text/URL contains a known anti-bot interstitial marker."""
    if not text:
        return False
    low = text.lower()
    return any(marker in low for marker in _ANTI_BOT_MARKERS)


def build_search_url(keyword: str) -> str:
    return SEARCH_URL_TEMPLATE.format(kw=quote(keyword))


def seed_profile_if_needed(context, profile_dir: Path, storage_state_path: Path | None) -> bool:
    """Seed a fresh persistent-context profile with cookies, exactly once.

    A profile with history/cookies looks like a real returning user; a fresh
    context every run does not (task spec §3.1). Only cookies are imported —
    Playwright's persistent-context API has no supported way to seed
    localStorage/origins ahead of a page load, and `li_at` (the session
    cookie) is what actually authenticates the request, so that's the part
    worth carrying over. Runs at most once per profile dir (marker file).

    Returns True if seeding actually ran (for tests / logging), False if the
    profile was already seeded or there was nothing to seed.
    """
    import json

    marker = Path(profile_dir) / _SEEDED_MARKER_NAME
    if marker.exists():
        return False
    if storage_state_path is None or not Path(storage_state_path).exists():
        logger.warning(
            "[linkedin_scout] no LINKEDIN_STORAGE_STATE to seed the profile from — "
            "run tools/linkedin_login.py first if this is a fresh profile."
        )
        return False
    try:
        data = json.loads(Path(storage_state_path).read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        if cookies:
            context.add_cookies(cookies)
            logger.info("[linkedin_scout] seeded %d cookies into new profile", len(cookies))
    except Exception as e:  # noqa: BLE001 — best-effort seed, never fatal
        logger.warning("[linkedin_scout] profile seeding failed: %s", e)
        return False
    finally:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    return True


# JS run inside the page to extract post text. Per the live-probe finding
# (plan §4.6 #3), `document.body.innerText` already renders open shadow DOM
# content on this surface — that's the primary source. The shadow-root walk
# below is a safety net for anything innerText might still miss, not the
# primary mechanism (the permalink-collection use case for the walker was
# ruled out in probe round 2 finding #5 — no permalinks are reachable at all).
_EXTRACT_JS = """
() => {
  function collectShadowText(root, out) {
    const all = root.querySelectorAll('*');
    all.forEach((el) => {
      if (el.shadowRoot) {
        out.push(el.shadowRoot.textContent || '');
        collectShadowText(el.shadowRoot, out);
      }
    });
  }
  const bodyText = document.body ? document.body.innerText : '';
  const shadowParts = [];
  if (document.body) {
    collectShadowText(document.body, shadowParts);
  }
  const shadowText = shadowParts.join('\\n');
  if (shadowText && !bodyText.includes(shadowText.slice(0, 40))) {
    return bodyText + '\\n' + shadowText;
  }
  return bodyText;
}
"""


def _sleep_human(range_sec: tuple[float, float]) -> None:
    time.sleep(random.uniform(*range_sec))


def _send_circuit_breaker_alert(reason: str) -> bool:
    """Direct, dependency-light Telegram send — the one alert the circuit
    breaker fires on trip (task spec §3.5). Best-effort; never raises."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.warning("[linkedin_scout] no Telegram configured — trip alert not sent: %s", reason)
        return False
    try:
        import requests

        text = (
            "⚠️ LinkedIn flagged the scout session — re-run "
            "tools/linkedin_login.py, then `python linkedin_scout/run.py --reset` "
            f"to resume.\n\nReason: {reason}"
        )
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text},
            timeout=10,
        )
        return resp.ok
    except Exception as e:  # noqa: BLE001
        logger.warning("[linkedin_scout] trip alert send failed: %s", e)
        return False


@dataclass
class ScoutCandidate:
    """A post that passed the M1 heuristic + location gate, ready for M3."""

    keyword: str
    author: str
    body: str
    scouted_at: str


def _open_scroll_extract(
    url: str,
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    headless: bool,
    scroll_iterations: int,
) -> str:
    """Shared mechanics: launch persistent context, seed, navigate, scroll,
    extract text. Raises AntiBotDetected on any login/checkpoint/authwall
    redirect or anti-bot interstitial — no retries, caller must not loop this.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            args=list(STEALTH_CHROME_ARGS),
        )
        try:
            seed_profile_if_needed(context, profile_dir, storage_state_path)
            context.add_init_script(_HIDE_WEBDRIVER_INIT_SCRIPT)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            _sleep_human(_POST_LOAD_WAIT_RANGE_SEC)

            if is_blocked_url(page.url):
                raise AntiBotDetected(f"redirected to {page.url}")

            for _ in range(scroll_iterations):
                page.mouse.wheel(0, 2000)
                _sleep_human(_SCROLL_WAIT_RANGE_SEC)
                if is_blocked_url(page.url):
                    raise AntiBotDetected(f"redirected to {page.url} during scroll")

            text = page.evaluate(_EXTRACT_JS) or ""
            if looks_like_anti_bot(text) or looks_like_anti_bot(page.url):
                raise AntiBotDetected("anti-bot interstitial marker detected in page")
            return text
        finally:
            context.close()


def scout_keyword(
    keyword: str,
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    headless: bool = False,
) -> str:
    """Open ONE content-search page for `keyword` and return the page text.

    Raises AntiBotDetected on a login/checkpoint/authwall redirect or a known
    anti-bot interstitial — the caller must not retry.
    """
    return _open_scroll_extract(
        build_search_url(keyword),
        profile_dir=profile_dir,
        storage_state_path=storage_state_path,
        headless=headless,
        scroll_iterations=_SCROLL_ITERATIONS,
    )


def scout_feed(
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    headless: bool = False,
) -> str:
    """Open the home feed (no keyword) and return the page text.

    Second, independent scout track (owner decision 2026-07-07): scrolls the
    main feed for ANY post rather than a keyword search. Uses its own
    (larger) scroll budget — "all posts" rather than one page of search
    results. Same AntiBotDetected contract as scout_keyword.
    """
    return _open_scroll_extract(
        FEED_URL,
        profile_dir=profile_dir,
        storage_state_path=storage_state_path,
        headless=headless,
        scroll_iterations=_FEED_SCROLL_ITERATIONS,
    )


def _filter_candidates(raw_text: str, label: str) -> list[ScoutCandidate]:
    """Parse raw page text into posts and keep only ones passing M1's gate."""
    posts: list[ParsedPost] = parse_posts(raw_text)
    scouted_at = datetime.now(timezone.utc).isoformat()
    candidates: list[ScoutCandidate] = []
    for post in posts:
        if not is_hiring_post(post.body):
            continue
        if check_location(post.body) is LocationVerdict.REJECT:
            continue
        candidates.append(
            ScoutCandidate(
                keyword=label,
                author=post.author,
                body=post.body,
                scouted_at=scouted_at,
            )
        )
    logger.info(
        "[linkedin_scout] '%s': %d posts parsed, %d candidates",
        label,
        len(posts),
        len(candidates),
    )
    return candidates


def _run_with_breaker(
    *,
    label: str,
    scout_call,
    state,
) -> list[ScoutCandidate]:
    """Shared circuit-breaker wiring for both the keyword-search and feed
    scout entry points: no-op while tripped, trip + alert exactly once on
    AntiBotDetected, otherwise filter the raw text through M1."""
    if state.is_tripped():
        logger.warning(
            "[linkedin_scout] circuit breaker is tripped (%s) — no-op until --reset",
            state.trip_reason(),
        )
        return []

    try:
        raw_text = scout_call()
    except AntiBotDetected as e:
        first_trip = state.trip(str(e))
        logger.error("[linkedin_scout] circuit breaker tripped: %s", e)
        if first_trip:
            _send_circuit_breaker_alert(str(e))
        return []

    return _filter_candidates(raw_text, label)


def run_once(
    keywords: list[str],
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    state,
    headless: bool = False,
) -> list[ScoutCandidate]:
    """One keyword-search scout invocation: pick the next rotation keyword,
    search, filter through M1.

    Circuit breaker: on AntiBotDetected, trips `state` and sends exactly one
    Telegram alert (only on the trip that actually flips tripped=False->True).
    Returns [] on any abort. Never raises for anti-bot conditions — that's the
    whole point of the breaker (log loudly, don't crash the scheduled task).
    """
    if state.is_tripped():
        logger.warning(
            "[linkedin_scout] circuit breaker is tripped (%s) — no-op until --reset",
            state.trip_reason(),
        )
        return []

    keyword = state.next_keyword(keywords)
    logger.info("[linkedin_scout] scouting keyword: %s", keyword)

    return _run_with_breaker(
        label=keyword,
        scout_call=lambda: scout_keyword(
            keyword,
            profile_dir=profile_dir,
            storage_state_path=storage_state_path,
            headless=headless,
        ),
        state=state,
    )


def run_feed_once(
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    state,
    headless: bool = False,
) -> list[ScoutCandidate]:
    """One home-feed scout invocation (owner's second track, 2026-07-07): no
    keyword, no rotation — just scroll the main feed and filter through M1.

    Uses its own `state`/`profile_dir` (a separate ScoutState instance and a
    separate persistent Chrome profile from the keyword-search track), so the
    two can run independently without fighting over the same profile lock or
    circuit-breaker flag — a trip on one track does not silence the other.
    """
    return _run_with_breaker(
        label="feed",
        scout_call=lambda: scout_feed(
            profile_dir=profile_dir,
            storage_state_path=storage_state_path,
            headless=headless,
        ),
        state=state,
    )
