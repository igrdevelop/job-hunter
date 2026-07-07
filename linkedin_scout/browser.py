"""Playwright scraping for the LinkedIn posts scout (M2).

Design per docs/LINKEDIN_POSTS_SCOUT_TASK.md §3.1/§3.5 and the live-probe
findings in docs/LINKEDIN_POSTS_SOURCE_PLAN.md §4.6 (branch
feat/linkedin-posts-source):

- Persistent Chrome profile (channel="chrome", headed), cookies re-seeded from
  LINKEDIN_STORAGE_STATE on every run (see seed_profile_cookies — NOT "once
  ever", that was tried and empirically disproved).
- ONE page load per keyword per run, human-paced waits, no parallelism.
- Circuit breaker: any login/checkpoint/authwall redirect or anti-bot response
  aborts immediately (no retries) and trips the persisted state (state.py) so
  every subsequent run no-ops until the owner clears it with `--reset`.

Playwright itself is imported lazily inside the functions that need it, so
this module (and its pure helpers) can be imported and unit-tested in an
environment without a real browser session available.

VERIFIED LOCALLY (2026-07-07, real Chrome via channel="chrome", zero network —
file:// fixtures only, see tests/test_linkedin_scout_extract_integration.py):
the full launch → seed → init-script → navigate → scroll → extract pipeline
(_open_scroll_extract) runs end-to-end without error against a real browser,
and _EXTRACT_JS correctly reads text out of real (including nested) open
shadow DOM while preserving the line breaks parser.parse_posts() depends on.
This caught and fixed two real bugs that unit tests (which mock the
Playwright API) could not have caught: (1) the original extraction JS assumed
`document.body.innerText` renders shadow DOM content — verified FALSE against
real Chrome; (2) the shadow-root fallback used `.textContent`, which drops
all line breaks and would have made every post unparseable.

NOT YET VERIFIED: anything that requires an actual LinkedIn session — the
real search-result DOM shape, whether cookie re-seeding is enough to pass
LinkedIn's own auth checks, and whether the stealth measures hold up against
LinkedIn's live anti-bot detection. Per the task spec, that verification is a
live run on the owner's own machine, not something this change can do.
`docs/LINKEDIN_POSTS_SOURCE_PLAN.md` documents that this DOM is the most
fragile of any scraper in the repo — expect it to need adjustment.
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
# page of results (owner decision 2026-07-07): scroll for up to ~10 minutes at
# a slower, more varied pace, stopping early once the feed plateaus (several
# scrolls in a row surface no new posts — LinkedIn ran out of fresh content,
# no point continuing). `_FEED_SCROLL_MAX_ITERATIONS` is only a hard safety
# ceiling in case the duration/plateau logic is ever misconfigured; in
# practice one of the other two limits fires first.
_FEED_SCROLL_MAX_ITERATIONS = 200
_FEED_SCROLL_MAX_DURATION_SEC = 600.0
_FEED_SCROLL_WAIT_RANGE_SEC = (2.0, 5.0)
_FEED_SCROLL_PLATEAU_LIMIT = 5

# search track only (owner decision 2026-07-07): the run is a few seconds,
# scheduled hourly, so it can fire while the owner is actively working. Move
# the (still fully rendered, headed — not headless) window off the visible
# desktop area instead of stealing focus every hour. Not used for the feed
# track, which runs for up to 10 minutes and the owner accepted just leaving
# visible-but-ignorable on screen (an off-screen window risks Chrome treating
# a long session as occluded/backgrounded and throttling lazy-loaded content).
_SEARCH_OFFSCREEN_ARGS: tuple[str, ...] = ("--window-position=-3000,0",)

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

# Owner decision (2026-07-08): search runs through the ENTIRE keyword list in
# one invocation now, not one keyword per run (the original anti-detection
# design) — a human-paced pause between each keyword's search within the
# same run.
_BETWEEN_KEYWORD_WAIT_RANGE_SEC = (10.0, 30.0)


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


def seed_profile_cookies(context, storage_state_path: Path | None) -> bool:
    """Inject cookies from LINKEDIN_STORAGE_STATE into the context.

    IMPORTANT — this runs on EVERY invocation, not just once per profile.
    Empirically verified (2026-07-07, local no-network test against a real
    Chrome persistent context): cookies injected via Playwright's
    `add_cookies()` on a `launch_persistent_context()` browser ARE correctly
    sent on real requests during the CURRENT session, but do NOT get written
    to the profile's on-disk cookie store — a fresh `launch_persistent_context`
    against the same `user_data_dir` comes back with zero cookies. So "seed
    once, let the profile own it going forward" (the original plan) silently
    produces an unauthenticated session on every run after the first. The
    profile directory still earns its keep for what DOES persist to disk
    normally (history, cache, localStorage) — it just isn't a substitute for
    re-injecting the auth cookie fresh, every run, from the canonical
    LINKEDIN_STORAGE_STATE file.

    Returns True if cookies were injected, False if there was nothing to seed
    (missing/absent storage_state, or a read/parse failure — best-effort).
    """
    import json

    if storage_state_path is None or not Path(storage_state_path).exists():
        logger.warning(
            "[linkedin_scout] no LINKEDIN_STORAGE_STATE to seed cookies from — "
            "run tools/linkedin_login.py first."
        )
        return False
    try:
        data = json.loads(Path(storage_state_path).read_text(encoding="utf-8"))
        cookies = data.get("cookies", [])
        if not cookies:
            return False
        context.add_cookies(cookies)
        logger.info("[linkedin_scout] seeded %d cookies for this run", len(cookies))
        return True
    except Exception as e:  # noqa: BLE001 — best-effort seed, never fatal
        logger.warning("[linkedin_scout] cookie seed failed: %s", e)
        return False


# JS run inside the page to extract post text.
#
# The plan's live-probe finding (§4.6 #3) claimed `document.body.innerText`
# already renders open shadow DOM content on this surface. Empirically
# verified FALSE (2026-07-07, local no-network test: a real open shadow root
# attached under document.body was completely invisible to
# `document.body.innerText`, which returned only the light-DOM text either
# side of it). So this walker is the PRIMARY extraction mechanism, not a
# safety net: it descends into `el.shadowRoot` wherever one exists, and calls
# `.innerText` on any subtree that contains no shadow root at all (cheap,
# preserves line breaks the parser's "Feed post" splitter depends on — plain
# `.textContent` does not, and was the previous bug here). `ownText()` grabs a
# shadow-hosting element's own direct text-node children before recursing, so
# a light-DOM text node sitting next to a shadow-hosting sibling isn't lost.
_EXTRACT_JS = """
() => {
  function hasShadowDescendant(el) {
    if (el.shadowRoot) return true;
    const kids = el.children ? Array.from(el.children) : [];
    return kids.some(hasShadowDescendant);
  }
  function ownText(el) {
    return Array.from(el.childNodes)
      .filter((n) => n.nodeType === 3)
      .map((n) => n.textContent.trim())
      .filter(Boolean)
      .join(' ');
  }
  function collect(root, out) {
    const children = root.children ? Array.from(root.children) : [];
    children.forEach((el) => {
      if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(el.tagName)) return;
      if (el.shadowRoot) {
        collect(el.shadowRoot, out);
        return;
      }
      if (hasShadowDescendant(el)) {
        const own = ownText(el);
        if (own) out.push(own);
        collect(el, out);
      } else {
        const t = el.innerText !== undefined ? el.innerText : (el.textContent || '');
        if (t && t.trim()) out.push(t.trim());
      }
    });
  }
  const out = [];
  collect(document.body, out);
  return out.join('\\n');
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
    # Only set when a plain `<a href="/in/...">` was readable on the actor
    # element without any extra click (task spec §3.2) — the current DOM
    # extraction (browser.py's document.body.innerText capture) doesn't carry
    # hrefs, so this is always None for now; wiring it up is future work if the
    # live DOM turns out to expose it cheaply. notify.py already renders it
    # when present so no further change is needed there once it's populated.
    author_profile_url: str | None = None


def _open_scroll_extract(
    url: str,
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    headless: bool,
    scroll_iterations: int,
    scroll_wait_range: tuple[float, float] = _SCROLL_WAIT_RANGE_SEC,
    max_duration_sec: float | None = None,
    plateau_limit: int | None = None,
    extra_chrome_args: tuple[str, ...] = (),
) -> str:
    """Shared mechanics: launch persistent context, seed, navigate, scroll,
    extract text. Raises AntiBotDetected on any login/checkpoint/authwall
    redirect or anti-bot interstitial — no retries, caller must not loop this.

    `scroll_iterations` is always a hard cap. `max_duration_sec` (if given)
    stops the loop early once that much wall-clock time has elapsed —
    intended for a long, slow feed-scroll session, not the short keyword-
    search burst. `plateau_limit` (if given) stops early once that many
    consecutive scrolls in a row surface no NEW posts (the feed ran out of
    fresh content — no point continuing to scroll past that). `extra_chrome_args`
    lets a caller (currently just the search track) append launch flags on top
    of `STEALTH_CHROME_ARGS`, e.g. an off-screen `--window-position` so the
    window doesn't steal focus during a short run.
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",
            headless=headless,
            args=[*STEALTH_CHROME_ARGS, *extra_chrome_args],
        )
        try:
            seed_profile_cookies(context, storage_state_path)
            context.add_init_script(_HIDE_WEBDRIVER_INIT_SCRIPT)
            page = context.new_page()
            page.goto(url, wait_until="domcontentloaded")
            _sleep_human(_POST_LOAD_WAIT_RANGE_SEC)

            if is_blocked_url(page.url):
                raise AntiBotDetected(f"redirected to {page.url}")

            # page.mouse.wheel() scrolls whatever is under the cursor — and
            # Playwright's mouse position defaults to nowhere-on-page until
            # mouse.move() is called at least once. Empirically verified
            # (2026-07-07, local no-network test): without this move(), every
            # subsequent wheel() call is silently a no-op — the page never
            # scrolls at all, which is exactly what the owner's first two live
            # runs showed (posts-visible count identical before/after scroll).
            viewport = page.viewport_size or {"width": 1280, "height": 800}
            page.mouse.move(viewport["width"] // 2, viewport["height"] // 2)

            text = page.evaluate(_EXTRACT_JS) or ""
            post_count = len(parse_posts(text))
            logger.info("[linkedin_scout] posts visible before scrolling: %d", post_count)

            start = time.monotonic()
            plateau_streak = 0
            iterations_done = 0
            while iterations_done < scroll_iterations:
                if max_duration_sec is not None and (time.monotonic() - start) >= max_duration_sec:
                    logger.info(
                        "[linkedin_scout] scroll time budget (%.0fs) reached after %d scroll(s)",
                        max_duration_sec, iterations_done,
                    )
                    break

                # Randomized scroll distance, not a robotic fixed step every time.
                page.mouse.wheel(0, random.randint(1200, 2600))
                _sleep_human(scroll_wait_range)
                iterations_done += 1
                if is_blocked_url(page.url):
                    raise AntiBotDetected(f"redirected to {page.url} during scroll")

                text = page.evaluate(_EXTRACT_JS) or ""
                if looks_like_anti_bot(text) or looks_like_anti_bot(page.url):
                    raise AntiBotDetected("anti-bot interstitial marker detected in page")

                new_count = len(parse_posts(text))
                if plateau_limit is not None:
                    plateau_streak = plateau_streak + 1 if new_count <= post_count else 0
                    post_count = new_count
                    if plateau_streak >= plateau_limit:
                        logger.info(
                            "[linkedin_scout] scroll plateaued (%d scroll(s) with no new posts) — "
                            "stopping early after %d/%d",
                            plateau_streak, iterations_done, scroll_iterations,
                        )
                        break
                else:
                    post_count = new_count

            logger.info(
                "[linkedin_scout] posts visible after %d scroll(s): %d",
                iterations_done, post_count,
            )
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
    anti-bot interstitial — the caller must not retry. Launches the (still
    headed) window off-screen (`_SEARCH_OFFSCREEN_ARGS`) so an hourly run
    doesn't steal focus from whatever the owner is doing.
    """
    return _open_scroll_extract(
        build_search_url(keyword),
        profile_dir=profile_dir,
        storage_state_path=storage_state_path,
        headless=headless,
        scroll_iterations=_SCROLL_ITERATIONS,
        extra_chrome_args=_SEARCH_OFFSCREEN_ARGS,
    )


def scout_feed(
    *,
    profile_dir: Path,
    storage_state_path: Path | None,
    headless: bool = False,
) -> str:
    """Open the home feed (no keyword) and scroll it for an extended session.

    Second, independent scout track (owner decision 2026-07-07): scrolls the
    main feed for ANY post rather than a keyword search — up to
    `_FEED_SCROLL_MAX_DURATION_SEC` (~10 minutes) at a slower, randomized
    pace, stopping early if the feed plateaus (`_FEED_SCROLL_PLATEAU_LIMIT`
    consecutive scrolls with no new posts). Same AntiBotDetected contract as
    scout_keyword.
    """
    return _open_scroll_extract(
        FEED_URL,
        profile_dir=profile_dir,
        storage_state_path=storage_state_path,
        headless=headless,
        scroll_iterations=_FEED_SCROLL_MAX_ITERATIONS,
        scroll_wait_range=_FEED_SCROLL_WAIT_RANGE_SEC,
        max_duration_sec=_FEED_SCROLL_MAX_DURATION_SEC,
        plateau_limit=_FEED_SCROLL_PLATEAU_LIMIT,
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
    """One search-track invocation: searches EVERY keyword in `keywords`, in a
    freshly randomized order each call (owner decision 2026-07-08 — the
    original design searched only one rotation-keyword per run in a fixed
    round-robin order; the owner asked first for the full list every time,
    then for that list to be in random order too, so consecutive runs don't
    always start the batch on the same keyword).

    Each keyword still gets its own full scout_keyword() call (own persistent-
    context launch/close), with a randomized human-paced pause between
    keywords (jitter — see `_BETWEEN_KEYWORD_WAIT_RANGE_SEC`). Circuit
    breaker: on AntiBotDetected, trips `state` and sends exactly one Telegram
    alert (only on the trip that actually flips tripped=False->True), and the
    loop stops immediately — no further keywords are attempted once tripped.
    Returns whatever candidates were collected before that point. Never raises
    for anti-bot conditions — that's the whole point of the breaker (log
    loudly, don't crash the scheduled task).
    """
    if state.is_tripped():
        logger.warning(
            "[linkedin_scout] circuit breaker is tripped (%s) — no-op until --reset",
            state.trip_reason(),
        )
        return []

    shuffled_keywords = list(keywords)
    random.shuffle(shuffled_keywords)

    all_candidates: list[ScoutCandidate] = []
    for i, keyword in enumerate(shuffled_keywords):
        logger.info(
            "[linkedin_scout] scouting keyword %d/%d: %s", i + 1, len(shuffled_keywords), keyword
        )

        candidates = _run_with_breaker(
            label=keyword,
            scout_call=lambda kw=keyword: scout_keyword(
                kw,
                profile_dir=profile_dir,
                storage_state_path=storage_state_path,
                headless=headless,
            ),
            state=state,
        )
        all_candidates.extend(candidates)

        if state.is_tripped():
            logger.warning(
                "[linkedin_scout] circuit breaker tripped mid-run — stopping remaining keywords"
            )
            break

        if i < len(shuffled_keywords) - 1:
            _sleep_human(_BETWEEN_KEYWORD_WAIT_RANGE_SEC)

    return all_candidates


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
