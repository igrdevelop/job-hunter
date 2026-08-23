"""
LinkedIn source — search jobs via LinkedIn's public guest API.

No authentication required for search. Uses HTML fragments from:
  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

Detail-page fetch has two paths:
  * ``fetch_text`` — unauthenticated HTML fallback (requests). Used by
    ``/check_expired``, gmail enricher, etc. Guest HTML omits the
    "No longer accepting applications" marker.
  * ``fetch_text_with_session`` — Playwright + ``LINKEDIN_STORAGE_STATE``.
    Used only by the apply pipeline (via ``fetch_job_text(..., use_session=True)``)
    so expired LinkedIn postings are detected before any LLM spend.
"""

import logging
import os
import re
import time
from html import unescape as html_unescape
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

SEARCH_API = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
TIMEOUT = 20
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html",
    "Accept-Language": "en-US,en;q=0.9",
}
# The guest search endpoint returns TEN cards per call, not 25 — measured
# 2026-08-12 against the live endpoint, with a control run first (three
# identical requests returned identical id sets, so 10 is the real page size,
# not a rotating sample). The old `RESULTS_PER_PAGE = 25` silently capped this
# source at a SINGLE call per keyword: `_search_keyword` stepped `start` by 25
# and broke out as soon as a page returned fewer than 25 rows, which 10 always
# does. Paginated properly, the same keyword/window yields ~69 postings where
# the bot was seeing 10 — see docs/AGENT_LOG.md for the measurement.
PAGE_STEP = 10
MAX_PAGES_PER_KEYWORD = 10  # ceiling ~100 postings per keyword
PAGE_DELAY_SEC = 1.5

# ── Detail-page fetch settings ──────────────────────────────────────────────
_STORAGE_STATE_ENV = "LINKEDIN_STORAGE_STATE"
_DETAIL_TIMEOUT_MS = 15_000
_DETAIL_MAX_TEXT_LEN = 15_000
_BLOCKED_RESOURCE_TYPES = frozenset({"image", "media", "font"})

# Same synthetic marker the findmyremote / thesmartjobs / Lever fetchers use:
# hunter.expired_check.is_job_expired matches "has expired", so a closed
# posting takes the clean $0 EXPIRED path instead of reaching the LLM.
_EXPIRED_TEXT = "This job posting has expired."

_CTA_CONTAINER_CLASS = "top-card-layout__cta-container"


def guest_html_expired(html: str) -> bool:
    """True when logged-out LinkedIn HTML shows a CLOSED posting.

    LinkedIn does NOT render "No longer accepting applications" for a guest —
    that banner exists only in the logged-in view, which is why the
    ``HTML_EXPIRED_MARKERS["linkedin.com"]`` substrings never fire on a guest
    page. Measured 2026-08-22 against 14 live postings and one closed one
    (jobs/view/4455428397): the single difference is the apply CTA. A live
    page puts an Apply button inside ``top-card-layout__cta-container``; a
    closed one renders that container EMPTY (``<!----> <!---->``). 14/14 live
    pages carried the button, the closed one carried none.

    Deliberately conservative in two ways, because a false positive silently
    EXPIREs a real vacancy: the container must be PRESENT (a renamed/removed
    container returns False rather than expiring every LinkedIn posting), and
    any link OR button inside counts as alive (so a class rename on the button
    itself is harmless).
    """
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency in prod
        logger.debug("[linkedin] beautifulsoup4 missing — skipping guest expiry check")
        return False

    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(class_=_CTA_CONTAINER_CLASS)
    if container is None:
        return False
    return container.find(["a", "button"]) is None


def _storage_state_path() -> Optional[Path]:
    val = os.environ.get(_STORAGE_STATE_ENV, "").strip()
    if not val:
        return None
    p = Path(val)
    return p if p.exists() else None


def _clean_detail_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


# ── URL parsing helpers (ported from job_fetch/linkedin_parse.py) ───────────


def is_linkedin_url(url: str) -> bool:
    """True for any linkedin.com URL."""
    return "linkedin.com" in (urlparse(url).hostname or "")


def is_linkedin_search(url: str) -> bool:
    """True if URL is a LinkedIn jobs search/alert page (not a single job view)."""
    parsed = urlparse(url)
    if "linkedin.com" not in (parsed.hostname or ""):
        return False
    return "/jobs/search" in parsed.path or "/jobs/search" in url


def is_linkedin_view(url: str) -> bool:
    """True if URL is already a single job view."""
    return "linkedin.com" in (urlparse(url).hostname or "") and "/jobs/view/" in url


def parse_linkedin_job_ids(url: str) -> list[str]:
    """Extract deduplicated job ids from a LinkedIn search / alert URL."""
    qs = parse_qs(urlparse(url).query, keep_blank_values=False)
    ids: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        for part in raw.replace("%2C", ",").split(","):
            jid = part.strip()
            if jid and jid not in seen:
                seen.add(jid)
                ids.append(jid)

    for val in qs.get("currentJobId", []):
        _add(val)
    for val in qs.get("originToLandingJobPostings", []):
        _add(val)
    for val in qs.get("jobIds", []):
        _add(val)
    return ids


def job_view_url(job_id: str) -> str:
    """Canonical URL for a single LinkedIn job posting."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/"


def normalize_linkedin_url(url: str) -> str:
    """Strip tracking params from a LinkedIn job view URL.

    Non-view URLs are returned unchanged.
    """
    parsed = urlparse(url)
    if "linkedin.com" not in (parsed.hostname or "") or "/jobs/view/" not in parsed.path:
        return url
    m = re.search(r"/jobs/view/(\d+)", parsed.path)
    if m:
        return f"https://www.linkedin.com/jobs/view/{m.group(1)}/"
    return parsed._replace(query="", fragment="").geturl()


class LinkedInSource(BaseSource):
    name = "linkedin"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "linkedin.com" in host

    def fetch_text(self, url: str) -> str:
        """Fetch a LinkedIn job posting without a logged-in session.

        Guest HTML omits the "No longer accepting applications" banner, but a
        closed posting is still recognisable by its empty apply-CTA container
        (``guest_html_expired``) — so this path returns the synthetic expired
        marker instead of a full description, and the caller's own
        ``is_job_expired`` check takes it from there. Without that, a closed
        posting reads as a perfectly normal one and gets a full LLM-generated
        application (Ebiquity, 2026-08-22).

        ``fetch_text_with_session`` is still preferred by the apply pipeline —
        the logged-in view carries the banner explicitly — but it silently
        falls back here whenever ``LINKEDIN_STORAGE_STATE`` is unset or stale.
        """
        from hunter.sources.html_fallback import (
            HEADERS as HTML_HEADERS,
            MAX_TEXT_LEN,
            TIMEOUT as HTML_TIMEOUT,
            extract_text,
        )

        resp = requests.get(url, headers=HTML_HEADERS, timeout=HTML_TIMEOUT)
        resp.raise_for_status()
        html = resp.text

        if guest_html_expired(html):
            logger.info("[linkedin] Closed posting (empty apply CTA in guest HTML): %s", url)
            return _EXPIRED_TEXT

        text = extract_text(html)
        if len(text) < 100:
            raise ValueError(f"Page at {url} returned too little text ({len(text)} chars)")
        if len(text) > MAX_TEXT_LEN:
            text = text[:MAX_TEXT_LEN] + "\n\n[... truncated ...]"
        return text

    def fetch_text_with_session(self, url: str) -> str:
        """Fetch via Playwright + ``LINKEDIN_STORAGE_STATE`` (apply pipeline).

        Falls back to ``fetch_text`` (unauthenticated) when the session file
        is missing, Playwright is unavailable, or the session fetch fails —
        best-effort so a flaky browser never breaks the apply pipeline.
        """
        storage_state = _storage_state_path()
        if not storage_state:
            logger.warning(
                "[linkedin] %s not set — falling back to HTML fetch. "
                "Run python tools/linkedin_login.py to enable session fetch "
                "(needed to detect expired LinkedIn postings).",
                _STORAGE_STATE_ENV,
            )
            return self.fetch_text(url)
        try:
            return self._playwright_fetch(url, storage_state)
        except Exception as e:
            logger.warning("[linkedin] Session fetch failed: %s, falling back", e)
            return self.fetch_text(url)

    def _playwright_fetch(self, url: str, storage_state: Path) -> str:
        """Load a job page with the saved LinkedIn session; return body text."""
        try:
            from playwright.sync_api import TimeoutError as PWTimeout
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright not installed — "
                "Install with: pip install playwright && playwright install chromium"
            ) from e

        logger.info("[linkedin] Fetching %s with session from %s", url, storage_state)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                ctx = browser.new_context(
                    storage_state=str(storage_state),
                    user_agent=HEADERS["User-Agent"],
                )
                page = ctx.new_page()
                page.route(
                    "**/*",
                    lambda route: (
                        route.abort()
                        if route.request.resource_type in _BLOCKED_RESOURCE_TYPES
                        else route.continue_()
                    ),
                )

                try:
                    page.goto(url, timeout=_DETAIL_TIMEOUT_MS, wait_until="domcontentloaded")
                except PWTimeout as e:
                    raise RuntimeError(f"LinkedIn page timed out: {url}") from e

                current = page.url
                if "linkedin.com/login" in current or "linkedin.com/checkpoint" in current:
                    raise RuntimeError(
                        "LinkedIn redirected to login page — session expired.\n"
                        "Re-run: python tools/linkedin_login.py  to refresh storage_state."
                    )

                try:
                    page.wait_for_selector(
                        ".jobs-description, .job-view-layout, .description__text",
                        timeout=10_000,
                    )
                except PWTimeout:
                    pass

                text = page.evaluate(
                    """() => {
                        const remove = ['script','style','nav','footer','header','noscript'];
                        remove.forEach(t => document.querySelectorAll(t).forEach(e => e.remove()));
                        return document.body ? document.body.innerText : '';
                    }"""
                )
            finally:
                browser.close()

        text = _clean_detail_text(text)
        if len(text) < 100:
            raise RuntimeError(
                f"LinkedIn page returned too little text ({len(text)} chars) for {url}"
            )
        if len(text) > _DETAIL_MAX_TEXT_LEN:
            text = text[:_DETAIL_MAX_TEXT_LEN] + "\n\n[... truncated ...]"

        logger.info("[linkedin] Got %d chars", len(text))
        return text

    def search(self) -> list[Job]:
        keywords_raw = os.environ.get(
            "LINKEDIN_KEYWORDS", "angular,angular developer,frontend angular"
        )
        geo_id = os.environ.get("LINKEDIN_GEO_ID", "105072130")  # Poland
        keywords_list = [kw.strip() for kw in keywords_raw.split(",") if kw.strip()]

        all_jobs: list[Job] = []
        for kw in keywords_list:
            try:
                jobs = self._search_keyword(kw, geo_id)
                all_jobs.extend(jobs)
                logger.info(f"[LinkedIn] keyword '{kw}': {len(jobs)} jobs")
            except Exception as e:
                logger.error(f"[LinkedIn] Error searching '{kw}': {e}")

        # Dedup by job id across keywords
        seen: set[str] = set()
        unique: list[Job] = []
        for j in all_jobs:
            jid = self._extract_job_id(j.url)
            key = jid or j.url
            if key not in seen:
                seen.add(key)
                unique.append(j)

        logger.info(f"[LinkedIn] Total: {len(all_jobs)} raw -> {len(unique)} unique")
        return unique

    def _search_keyword(self, keyword: str, geo_id: str) -> list[Job]:
        """Walk the guest search pages for one keyword.

        Stops on an EMPTY page, never on a merely SHORT one: `start` offsets sit
        on a fixed `PAGE_STEP` grid and the last populated page is routinely
        short (measured sequence: 10, 10, 10, 10, 10, 10, 9, then 0), so "fewer
        rows than a full page" does not mean "no more results". That exact
        assumption is what used to end pagination on the first call.
        """
        jobs: list[Job] = []
        seen_ids: set[str] = set()
        pages_walked = 0

        for page in range(MAX_PAGES_PER_KEYWORD):
            if page:
                time.sleep(PAGE_DELAY_SEC)
            page_jobs = self._fetch_page(keyword, geo_id, page * PAGE_STEP)
            pages_walked += 1
            if not page_jobs:
                break
            for job in page_jobs:
                # Overlapping offsets can repeat a posting; the caller dedups
                # across keywords, this keeps one keyword's own list clean.
                jid = self._extract_job_id(job.url) or job.url
                if jid in seen_ids:
                    continue
                seen_ids.add(jid)
                jobs.append(job)

        logger.info(f"[LinkedIn] '{keyword}': {len(jobs)} unique over {pages_walked} page(s)")
        return jobs

    def _fetch_page(self, keyword: str, geo_id: str, start: int) -> list[Job]:
        # Deliberately NOT sent (measured live 2026-08-12): the guest endpoint
        # IGNORES both `f_E` (experience level) and `f_WT` (workplace type) —
        # passing either returns a byte-identical id set. `f_E=3,4` used to sit
        # here doing nothing; `f_WT` cannot express the remote/hybrid/Wrocław
        # preference on this endpoint at all, so that stays the central filter's
        # job (hunter/filters.py). Only an authenticated search honours them —
        # don't "restore" these without re-measuring.
        params = {
            "keywords": keyword,
            "location": "Poland",
            "geoId": geo_id,
            # The window IS honoured (24h vs 7d shared only 14 of 57/69 ids),
            # and the 7-day set is far more on-target: 49% of its rows carry
            # "angular" in the title versus 11% for 24h. URL dedup in the hunt
            # loop makes the wider window free.
            "f_TPR": os.environ.get("LINKEDIN_TPR", "").strip() or "r604800",
            "sortBy": "DD",  # most recent
            "start": str(start),
        }

        resp = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.error(f"[LinkedIn] API returned {resp.status_code}")
            return []

        return self._parse_html(resp.text)

    def _parse_html(self, html: str) -> list[Job]:
        """Parse HTML fragments from the guest search API."""
        titles = re.findall(r"<h3[^>]*base-search-card__title[^>]*>\s*(.*?)\s*</h3>", html, re.S)
        companies = re.findall(
            r"<h4[^>]*base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*(.*?)\s*</a>",
            html,
            re.S,
        )
        locations = re.findall(
            r"<span[^>]*job-search-card__location[^>]*>\s*(.*?)\s*</span>", html, re.S
        )
        job_ids = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html)

        jobs: list[Job] = []
        for i in range(len(job_ids)):
            title = html_unescape(titles[i].strip()) if i < len(titles) else ""
            company = html_unescape(companies[i].strip()) if i < len(companies) else "Unknown"
            location = html_unescape(locations[i].strip()) if i < len(locations) else "Unknown"
            job_id = job_ids[i]

            if not title:
                continue

            # Strip company name suffix: "Senior Dev / VBET" -> "Senior Dev"
            if company:
                title = re.sub(
                    r"\s*[-/|]\s*" + re.escape(company.strip()) + r"\s*$",
                    "",
                    title,
                    flags=re.I,
                ).strip()

            jobs.append(
                Job(
                    title=title,
                    company=company,
                    location=location,
                    salary=None,
                    url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                    source=self.name,
                    raw={"jobId": job_id},
                )
            )

        return jobs

    @staticmethod
    def _extract_job_id(url: str) -> Optional[str]:
        m = re.search(r"/jobs/view/(\d+)", url)
        return m.group(1) if m else None
