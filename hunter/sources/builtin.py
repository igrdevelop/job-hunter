"""
Built In (builtin.com) — large US/remote tech job board.

Strategy: the listing is server-rendered HTML behind Cloudflare (no public JSON
API, no __NEXT_DATA__, no JSON-LD on listings). Job cards carry stable
``data-id`` markers (``job-card``, ``company-title``, ``job-card-title``), so we
parse the DOM with BeautifulSoup. cloudscraper is used to ride through the
Cloudflare edge.

We query the remote dev/engineering category with ?search= terms, so every card
is remote-eligible. Detail-page text uses the generic html_fallback (the detail
DOM has no structured data either, but extracts cleanly).

Listing URL: https://builtin.com/jobs/remote/dev-engineering?search={term}
Job URL:     https://builtin.com/job/{slug}/{id}
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urljoin, urlparse

import cloudscraper

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://builtin.com"
LISTING_TMPL = "https://builtin.com/jobs/remote/dev-engineering?search={term}"
SEARCH_TERMS = ("angular", "frontend", "react")
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
TIMEOUT = 30

# Work-arrangement labels shown on a card. fullmatch-only so we never mistake a
# job title that merely contains the word "Remote" for the arrangement field.
_ARRANGEMENT_RE = re.compile(
    r"(fully remote|remote or hybrid|in-office or remote|hybrid or remote|"
    r"in-office or hybrid|remote|hybrid|in-office|on-?site)",
    re.I,
)

_scraper = cloudscraper.create_scraper()


class BuiltInSource(BaseSource):
    name = "builtin"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "builtin.com" in host

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for term in SEARCH_TERMS:
            url = LISTING_TMPL.format(term=term)
            try:
                raw_cards = self._fetch_cards(url)
            except Exception as e:
                logger.warning(f"[builtin] listing failed, skipping {url}: {e}")
                continue
            logger.info(f"[builtin] {url} -> {len(raw_cards)} cards")
            for raw in raw_cards:
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                if not self.matches_coarse_prefilter(job.title, raw.get("location", "")):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[builtin] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_cards(self, url: str) -> list[dict]:
        resp = _scraper.get(url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return parse_builtin_cards(resp.text)

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        href = (raw.get("href") or "").strip()
        if not title or not href:
            return None
        return Job(
            title=title,
            company=(raw.get("company") or "").strip() or "Unknown",
            location=(raw.get("location") or "").strip() or "Remote",
            salary=None,
            url=urljoin(BASE, href),
            source=self.name,
            raw=raw,
        )


def _arrangement(card) -> str:
    """Return the work-arrangement label from a card, or '' if none."""
    for s in card.find_all(string=True):
        text = s.strip()
        if text and _ARRANGEMENT_RE.fullmatch(text):
            return text
    return ""


def parse_builtin_cards(html: str) -> list[dict]:
    """Extract job-card dicts from a Built In listing page (no network)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    for card in soup.find_all(attrs={"data-id": "job-card"}):
        title_el = card.find(attrs={"data-id": "job-card-title"})
        if not title_el or not title_el.get("href"):
            continue
        company_el = card.find(attrs={"data-id": "company-title"})
        results.append(
            {
                "title": title_el.get_text(strip=True),
                "href": title_el.get("href"),
                "company": company_el.get_text(strip=True) if company_el else "",
                "location": _arrangement(card),
            }
        )
    return results
