"""
LinkedIn source — search jobs via LinkedIn's public guest API.

No authentication required for search. Uses HTML fragments from:
  https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

For fetching full job text (in apply_agent), storage_state.json is still used
via job_fetch/linkedin.py — but the search itself works without login.
"""

import logging
import os
import re
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
RESULTS_PER_PAGE = 25

# ── Detail-page fetch settings (ported from job_fetch/linkedin.py) ──────────
_STORAGE_STATE_ENV = "LINKEDIN_STORAGE_STATE"
_DETAIL_TIMEOUT_MS = 20_000
_DETAIL_MAX_TEXT_LEN = 15_000


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
        """Fetch a LinkedIn job posting via Playwright + saved session.

        Falls back to generic html_fallback when:
          * playwright is not installed
          * LINKEDIN_STORAGE_STATE is not configured

        Raises RuntimeError when LinkedIn redirects to login (session expired)
        or when the page times out / returns near-empty text.
        """
        from hunter.sources.html_fallback import fetch_html

        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
        except ImportError:
            logger.warning(
                "[linkedin] playwright not installed — falling back to HTML fetch. "
                "Install with: pip install playwright && playwright install chromium"
            )
            return fetch_html(url)

        storage_state = _storage_state_path()
        if not storage_state:
            logger.warning(
                f"[linkedin] {_STORAGE_STATE_ENV} not set — falling back to HTML fetch. "
                f"Run python tools/linkedin_login.py to enable full session fetch."
            )
            return fetch_html(url)

        logger.info(f"[linkedin] Fetching {url} with session from {storage_state}")

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                storage_state=str(storage_state),
                user_agent=HEADERS["User-Agent"],
            )
            page = ctx.new_page()

            try:
                page.goto(url, timeout=_DETAIL_TIMEOUT_MS, wait_until="domcontentloaded")
            except PWTimeout:
                browser.close()
                raise RuntimeError(f"LinkedIn page timed out: {url}")

            current = page.url
            if "linkedin.com/login" in current or "linkedin.com/checkpoint" in current:
                browser.close()
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

            browser.close()

        text = _clean_detail_text(text)
        if len(text) < 100:
            raise RuntimeError(
                f"LinkedIn page returned too little text ({len(text)} chars) for {url}"
            )
        if len(text) > _DETAIL_MAX_TEXT_LEN:
            text = text[:_DETAIL_MAX_TEXT_LEN] + "\n\n[... truncated ...]"

        logger.info(f"[linkedin] Got {len(text)} chars")
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
        """Fetch up to 2 pages (50 results) for a single keyword."""
        jobs: list[Job] = []
        for start in (0, RESULTS_PER_PAGE):
            page_jobs = self._fetch_page(keyword, geo_id, start)
            jobs.extend(page_jobs)
            if len(page_jobs) < RESULTS_PER_PAGE:
                break  # no more results
        return jobs

    def _fetch_page(self, keyword: str, geo_id: str, start: int) -> list[Job]:
        params = {
            "keywords": keyword,
            "location": "Poland",
            "geoId": geo_id,
            "f_TPR": "r86400",  # last 24 hours
            "f_E": "3,4",  # mid + senior
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
