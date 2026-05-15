"""
JustJoin.it source.

Working approach (updated 2026-05):

  JustJoin removed job slugs from SSR HTML — the listing pages are now fully
  client-side rendered. The candidate API works directly:

  GET /api/candidate-api/offers?workplaceType=remote&perPage=100
  → JSON {data: [...], meta: {next: {cursor, itemsCount}}}

  Strategy:
  1. Paginate /api/candidate-api/offers for each workplaceType
     (remote, hybrid, office) — up to MAX_PAGES pages of PER_PAGE items
  2. Pre-filter by slug keyword (same as before)
  3. Parse Job objects directly from the listing response (no detail call needed —
     the listing API returns full salary/location/skills data)
  4. City filtering is handled by the global filter (location field)

Rate: 3–6 API calls per run (1–2 pages × 3 workplace types).
"""

import logging
import time
from typing import Optional

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://justjoin.it"
LISTING_API = f"{BASE}/api/candidate-api/offers"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
JSON_HEADERS = {**HEADERS, "Accept": "application/json, text/plain, */*"}

TIMEOUT = 20
PER_PAGE = 100    # items per API page
MAX_PAGES = 2     # max pages per workplaceType — 200 items each, ~600 total
PAGE_DELAY = 0.3  # seconds between pages

WORKPLACE_TYPES = ["remote", "hybrid", "office"]


class JustJoinSource(BaseSource):
    name = "justjoin"

    def search(self) -> list[Job]:
        seen_slugs: set[str] = set()
        jobs: list[Job] = []

        for wtype in WORKPLACE_TYPES:
            cursor = None
            for _ in range(MAX_PAGES):
                params: dict = {"workplaceType": wtype, "perPage": PER_PAGE}
                if cursor:
                    params["cursor"] = cursor

                try:
                    resp = requests.get(
                        LISTING_API, params=params, headers=JSON_HEADERS, timeout=TIMEOUT
                    )
                    resp.raise_for_status()
                    body = resp.json()
                except Exception as e:
                    logger.error(f"[JustJoin] API fetch failed wtype={wtype}: {e}")
                    break

                for offer in body.get("data") or []:
                    slug = offer.get("slug", "")
                    if not slug or slug in seen_slugs:
                        continue
                    if not self._slug_is_relevant(slug):
                        continue
                    seen_slugs.add(slug)
                    job = self._parse_offer(offer, slug, wtype)
                    if job:
                        jobs.append(job)

                next_info = (body.get("meta") or {}).get("next") or {}
                cursor = next_info.get("cursor")
                if not cursor or next_info.get("itemsCount", 0) == 0:
                    break
                time.sleep(PAGE_DELAY)

        logger.info(f"[JustJoin] {len(jobs)} jobs fetched")
        return jobs

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
