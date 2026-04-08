"""
JustJoin.it source.

Public REST API was shut down Nov 2023.
Current working approach (tested 2026-04):

  1. Fetch SSR HTML listing pages (Wrocław + Remote) — gives ~150-200 slugs each
  2. Pre-filter slugs by keyword (angular/react/js/frontend/typescript in slug text)
     → reduces to ~30-50 relevant slugs
  3. Fetch individual offer details via  /api/candidate-api/offers/{slug}
     → clean JSON with salary, location, experience level, skills
  4. Return Job objects for global filter + dedup

Rate: ~30-50 HTTP calls per run — well within limits.
"""

import logging
import re
import time
from typing import Optional

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://justjoin.it"
DETAIL_API = f"{BASE}/api/candidate-api/offers"

# Listing pages — SSR HTML, no auth needed
LISTING_PAGES = [
    # Wrocław (on-site + hybrid jobs posted for that city)
    f"{BASE}/job-offers/wroclaw?experience-level=mid,senior&orderBy=DESC&sortBy=published",
    # Remote (covers all of Poland remote, incl. Wrocław remote)
    f"{BASE}/job-offers/remote?experience-level=mid,senior&orderBy=DESC&sortBy=published",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
HTML_HEADERS = {**HEADERS, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8"}
JSON_HEADERS = {**HEADERS, "Accept": "application/json, text/plain, */*"}

TIMEOUT = 20
DETAIL_DELAY = 0.15  # seconds between detail requests — be polite


class JustJoinSource(BaseSource):
    name = "justjoin"

    def search(self) -> list[Job]:
        # Step 1 — collect unique slugs from all listing pages
        relevant_slugs: dict[str, str] = {}  # slug → page context ("wroclaw" / "remote")

        for page_url in LISTING_PAGES:
            context = "remote" if "remote" in page_url else "wroclaw"
            slugs = self._fetch_slugs(page_url)
            logger.info(f"[JustJoin] {context}: {len(slugs)} total slugs")

            for slug in slugs:
                if slug not in relevant_slugs and self._slug_is_relevant(slug):
                    relevant_slugs[slug] = context

        logger.info(f"[JustJoin] {len(relevant_slugs)} slugs match keyword pre-filter")

        # Step 2 — fetch details and build Job objects
        jobs: list[Job] = []
        for slug, context in relevant_slugs.items():
            job = self._fetch_detail(slug, context)
            if job:
                jobs.append(job)
            time.sleep(DETAIL_DELAY)

        logger.info(f"[JustJoin] {len(jobs)} jobs fetched successfully")
        return jobs

    # ── Listing page scraper ───────────────────────────────────────────────────

    def _fetch_slugs(self, url: str) -> list[str]:
        try:
            resp = requests.get(url, headers=HTML_HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            # Extract all /job-offer/{slug} hrefs — deduplicated, order preserved
            slugs = list(dict.fromkeys(
                re.findall(r'href=["\'](?:https://justjoin\.it)?/job-offer/([a-z0-9-]+)["\']',
                           resp.text)
            ))
            return slugs
        except Exception as e:
            logger.error(f"[JustJoin] fetch slugs from {url}: {e}")
            return []

    def _slug_is_relevant(self, slug: str) -> bool:
        """
        Fast pre-filter on slug text (format: company-job-title-city-category).
        When require_angular is on, slug must contain 'angular'.
        Otherwise, match any title_keyword.
        """
        s = slug.lower()
        if FILTER.get("require_angular", False):
            return "angular" in s
        keywords = [kw.lower() for kw in FILTER["title_keywords"]]
        return any(kw in s for kw in keywords)

    # ── Detail fetcher ────────────────────────────────────────────────────────

    def _fetch_detail(self, slug: str, page_context: str) -> Optional[Job]:
        try:
            resp = requests.get(f"{DETAIL_API}/{slug}", headers=JSON_HEADERS, timeout=TIMEOUT)
            if resp.status_code == 404:
                return None  # offer expired
            resp.raise_for_status()
            offer = resp.json()
            return self._parse_offer(offer, slug, page_context)
        except Exception as e:
            logger.warning(f"[JustJoin] detail fetch failed for {slug}: {e}")
            return None

    # ── Parser ─────────────────────────────────────────────────────────────────

    def _parse_offer(self, offer: dict, slug: str, page_context: str) -> Optional[Job]:
        title = (offer.get("title") or "").strip()
        company = (offer.get("companyName") or "").strip()

        if not title or not company:
            return None

        url = f"{BASE}/job-offer/{slug}"
        location = self._parse_location(offer, page_context)
        salary = self._parse_salary(offer)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=offer,
        )

    @staticmethod
    def _parse_location(offer: dict, page_context: str) -> str:
        city = (offer.get("city") or "").strip()
        workplace = (offer.get("workplaceType") or "").lower()

        if workplace == "remote":
            return "Remote"
        if workplace == "hybrid":
            return f"{city} (Hybrid)" if city else "Hybrid"
        if workplace == "office":
            return f"{city} (On-site)" if city else "On-site"

        # Fallback from page context
        if page_context == "remote":
            return "Remote"
        return city or "Unknown"

    @staticmethod
    def _parse_salary(offer: dict) -> Optional[str]:
        """
        JustJoin candidate API salary format (2024+):
        employmentTypes: [{"from": 15000, "to": 20000, "currency": "PLN",
                           "fromPerUnit": 89.3, "toPerUnit": 119.0,
                           "type": "b2b", ...}]
        We prefer the monthly rate if available, show hourly if not.
        """
        emp_types = offer.get("employmentTypes") or []
        if not emp_types:
            return None

        # Take first type that has salary info
        for et in emp_types:
            low = et.get("from")
            high = et.get("to")
            currency = (et.get("currency") or "PLN").upper()
            emp_type = (et.get("type") or "").upper()

            if low or high:
                if low and high:
                    amount = f"{int(low):,}–{int(high):,}".replace(",", " ")
                elif low:
                    amount = f"{int(low):,}+".replace(",", " ")
                else:
                    amount = f"do {int(high):,}".replace(",", " ")

                label = f"{amount} {currency}"
                if emp_type:
                    label += f" {emp_type}"
                return label

        return None
