"""
jobleads.com source — international job aggregator with strong PL coverage.

Strategy (tested 2026-04):
  The listing pages are server-side rendered — job cards are present in the
  HTML response without JavaScript execution. No __NEXT_DATA__ or public API.
  We use cloudscraper (handles Cloudflare) + BeautifulSoup DOM parsing.

  Card DOM structure:
    div[data-testid="seo-search-list-job-card-{N}"]
      h2                                    → title
      a[data-testid="search-job-card-link"] → absolute job URL
      p[data-testid="search-job-card-company"] > span  → company
      div[data-testid="search-job-card-chips"]
        div[data-testid="job-card-chip-location"]  → city / country
        remaining chips → work type (Hybrid/Remote/On-site), salary, contract

Listing URLs:
  https://www.jobleads.com/pl/jobs?q=angular&location=wroclaw
  https://www.jobleads.com/pl/jobs?q=frontend&location=wroclaw
  https://www.jobleads.com/pl/jobs?q=angular&location=poland
"""

import logging
import re
from typing import Optional

import cloudscraper
from bs4 import BeautifulSoup

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://www.jobleads.com"

LISTING_URLS = [
    f"{BASE}/pl/jobs?q=angular&location=wroclaw",
    f"{BASE}/pl/jobs?q=frontend&location=wroclaw",
    f"{BASE}/pl/jobs?q=angular&location=poland",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()

# Salary-like text: currency symbol + digits OR digits + currency symbol
# Covers both "PLN 168,000 - 275,000" and "168 000 - 275 000 PLN"
_SALARY_RE = re.compile(
    r"(PLN|USD|EUR|GBP)\s*[\d\s,.-]+\d|\d[\d\s,.]*(PLN|USD|EUR|GBP)",
    re.I,
)

# Work-type chip labels (used to detect remote/hybrid for location building)
_REMOTE_LABELS = {"remote", "fully remote", "zdalnie"}
_HYBRID_LABELS = {"hybrid", "hybrydowo", "hybrydowa"}


class JobLeadsSource(BaseSource):
    name = "jobleads"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for listing_url in LISTING_URLS:
            try:
                raw_jobs = self._fetch_listing(listing_url)
                logger.info(f"[jobleads] {listing_url} -> {len(raw_jobs)} raw")
                for raw in raw_jobs:
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    if not self._is_relevant(raw, job):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
            except Exception as e:
                logger.warning(f"[jobleads] listing failed, skipping {listing_url}: {e}")

        logger.info(f"[jobleads] {len(jobs)} jobs after pre-filter")
        return jobs

    # ── Listing fetch ──────────────────────────────────────────────────────────

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[jobleads] HTTP failed for {url}: {e}")
            return []

        return self._parse_cards(resp.text)

    @staticmethod
    def _parse_cards(html: str) -> list[dict]:
        """Extract job card dicts from listing page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(
            "div",
            attrs={"data-testid": re.compile(r"^seo-search-list-job-card-\d+$")},
        )
        results = []
        for card in cards:
            raw = JobLeadsSource._extract_card(card)
            if raw:
                results.append(raw)
        return results

    @staticmethod
    def _extract_card(card) -> Optional[dict]:
        """Pull structured fields from a single job card element."""
        # Title
        h2 = card.find("h2")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            return None

        # Absolute URL
        link = card.find("a", attrs={"data-testid": "search-job-card-link"})
        url = link["href"].split("?")[0] if link and link.get("href") else ""
        if not url:
            return None

        # Company
        company_el = card.find("p", attrs={"data-testid": "search-job-card-company"})
        company = company_el.get_text(strip=True) if company_el else ""

        # Chips: location + work type + salary
        chips_el = card.find("div", attrs={"data-testid": "search-job-card-chips"})
        location = ""
        work_type = ""
        salary = ""
        if chips_el:
            loc_el = chips_el.find("div", attrs={"data-testid": "job-card-chip-location"})
            location = loc_el.get_text(strip=True) if loc_el else ""

            # Remaining chips: detect work type and salary by content
            for chip in chips_el.find_all("div", recursive=True):
                text = chip.get_text(strip=True)
                if not text or chip == loc_el:
                    continue
                if _SALARY_RE.search(text):
                    salary = salary or text
                elif text.lower() in _REMOTE_LABELS | _HYBRID_LABELS | {"on-site", "on site", "stacjonarnie"}:
                    work_type = work_type or text

        return {
            "title": title,
            "url": url,
            "company": company,
            "location": location,
            "work_type": work_type,
            "salary": salary,
            "_text": f"{title} {company} {location} {work_type}",
        }

    # ── Pre-filter ─────────────────────────────────────────────────────────────

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()
        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, title, re.I):
                return False
        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = (raw.get("_text") or "").lower()
        return any(kw in title + " " + text for kw in keywords)

    # ── Parser ─────────────────────────────────────────────────────────────────

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        if not title:
            return None

        url = (raw.get("url") or "").strip()
        if not url or not url.startswith("http"):
            return None

        company = (raw.get("company") or "Unknown").strip() or "Unknown"
        location = self._build_location(raw)
        salary = (raw.get("salary") or None)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _build_location(raw: dict) -> str:
        city = (raw.get("location") or "").strip()
        work_type = (raw.get("work_type") or "").strip().lower()

        if work_type in _REMOTE_LABELS:
            return f"{city} (Remote)" if city else "Remote"
        if work_type in _HYBRID_LABELS:
            return f"{city} (Hybrid)" if city else "Hybrid"
        if city:
            return city
        return "Unknown"
