"""
Pracuj.pl source (it.pracuj.pl sub-domain for IT jobs).

Strategy:
  1. Fetch listing pages for frontend/angular jobs in Wroclaw + remote.
     Pracuj.pl renders __NEXT_DATA__ JSON on SSR pages with job listing data.
  2. If __NEXT_DATA__ is unavailable, fall back to BeautifulSoup DOM parsing.
  3. Individual job text is fetched lazily by job_fetch/pracuj.py when LLM
     processing is triggered.

Listing URLs:
  https://it.pracuj.pl/praca/frontend;kw/wroclaw;wp?rd=30
  https://it.pracuj.pl/praca/angular;kw/wroclaw;wp?rd=30
  https://it.pracuj.pl/praca/frontend;kw?rd=0&remote=true
"""

import json
import logging
import re
from typing import Optional

import cloudscraper

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.tracker import normalize_url

logger = logging.getLogger(__name__)

BASE = "https://it.pracuj.pl"
OFFER_BASE = "https://www.pracuj.pl"

LISTING_URLS = [
    f"{BASE}/praca/frontend;kw/wroclaw;wp?rd=30",
    f"{BASE}/praca/angular;kw/wroclaw;wp?rd=30",
    f"{BASE}/praca/frontend;kw?rd=0&remote=true",
]

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()


class PracujSource(BaseSource):
    name = "pracuj"

    def search(self) -> list[Job]:
        seen_norm_urls: set[str] = set()
        seen_group_ids: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            raw_jobs = self._fetch_listing(url)
            logger.info(f"[Pracuj] {url} -> {len(raw_jobs)} raw jobs")
            for raw in raw_jobs:
                job = self._parse(raw)
                if not job:
                    continue
                if not self._is_relevant(raw, job):
                    continue
                gid = raw.get("groupId")
                if gid is not None and str(gid).strip():
                    gs = str(gid).strip()
                    if gs in seen_group_ids:
                        continue
                    seen_group_ids.add(gs)
                nu = normalize_url(job.url)
                if nu in seen_norm_urls:
                    continue
                seen_norm_urls.add(nu)
                jobs.append(job)

        logger.info(f"[Pracuj] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- Listing fetch ---------------------------------------------------------

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[Pracuj] fetch listing {url}: {e}")
            return []

        # Try __NEXT_DATA__ first
        jobs = self._extract_next_data(resp.text)
        if jobs:
            return jobs

        # Fallback: JSON-LD listing
        jobs = self._extract_json_ld(resp.text)
        if jobs:
            return jobs

        # Fallback: BeautifulSoup DOM
        return self._extract_bs4(resp.text)

    def _extract_next_data(self, html: str) -> list[dict]:
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, re.S,
        )
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        page_props = data.get("props", {}).get("pageProps", {})

        # Legacy format: offers directly in pageProps
        for key in ("offers", "data", "groupedOffers", "results"):
            offers = page_props.get(key)
            if isinstance(offers, list) and offers:
                return offers
            if isinstance(offers, dict):
                for sub_key in ("offers", "items", "results"):
                    sub = offers.get(sub_key)
                    if isinstance(sub, list) and sub:
                        return sub

        # Current format (2025+): React Query dehydratedState
        offers = self._extract_dehydrated_state(page_props)
        if offers:
            return offers

        return self._find_offers_in_props(page_props)

    @staticmethod
    def _extract_dehydrated_state(page_props: dict) -> list[dict]:
        """Extract offers from React Query dehydratedState cache."""
        ds = page_props.get("dehydratedState", {})
        queries = ds.get("queries", [])

        all_offers = []
        for query in queries:
            state = query.get("state", {})
            qdata = state.get("data")
            if not isinstance(qdata, dict):
                continue
            for key in ("groupedOffers", "offers", "results", "items"):
                items = qdata.get(key)
                if isinstance(items, list) and items:
                    if isinstance(items[0], dict) and any(
                        k in items[0] for k in ("jobTitle", "title", "companyName", "offerUrl")
                    ):
                        all_offers.extend(items)
        return all_offers

    @staticmethod
    def _find_offers_in_props(props: dict) -> list[dict]:
        """Walk pageProps looking for a list of job-like dicts."""
        for val in props.values():
            if not isinstance(val, list) or len(val) < 2:
                continue
            sample = val[0]
            if isinstance(sample, dict) and any(
                k in sample for k in ("jobTitle", "title", "companyName", "offerUrl", "uri")
            ):
                return val
        return []

    def _extract_json_ld(self, html: str) -> list[dict]:
        matches = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S,
        )
        results = []
        for raw in matches:
            try:
                data = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "ItemList":
                    for elem in item.get("itemListElement", []):
                        if isinstance(elem, dict):
                            results.append(elem)
                elif item.get("@type") == "JobPosting":
                    results.append(item)
        return results

    def _extract_bs4(self, html: str) -> list[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        # Pracuj listing pages use <a> tags with data-test or specific classes for offers
        offer_links = soup.find_all("a", href=re.compile(r",oferta,\d+"))
        seen = set()
        for a_tag in offer_links:
            href = a_tag.get("href", "")
            if href in seen:
                continue
            seen.add(href)

            title_el = a_tag.find(["h2", "h3", "span", "div"], string=True)
            title = title_el.get_text(strip=True) if title_el else a_tag.get_text(strip=True)

            if not title or len(title) < 3:
                continue

            jobs.append({
                "jobTitle": title,
                "offerUrl": href,
                "_source": "bs4",
            })

        return jobs

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()

        exclude_levels = [lv.lower() for lv in FILTER.get("exclude_levels", [])]
        level = (
            raw.get("experienceLevel") or raw.get("positionLevels") or ""
        )
        if isinstance(level, list):
            level = " ".join(str(l).lower() for l in level)
        else:
            level = str(level).lower()
        if any(lv in level for lv in exclude_levels):
            return False

        exclude_patterns = FILTER.get("exclude_patterns", [])
        for pat in exclude_patterns:
            if re.search(pat, title, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        return any(kw in title for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        # Handle both __NEXT_DATA__ format and JSON-LD format
        title = (
            raw.get("jobTitle")
            or raw.get("title")
            or raw.get("name")
            or ""
        ).strip()

        company = (
            raw.get("companyName")
            or raw.get("employer")
            or raw.get("company")
            or ""
        )
        if isinstance(company, dict):
            company = company.get("name", "")
        company = str(company).strip()

        if not title:
            return None

        url = self._build_url(raw)
        if not url:
            return None

        location = self._parse_location(raw)
        salary = self._parse_salary(raw)

        return Job(
            title=title,
            company=company or "Unknown",
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _build_url(raw: dict) -> str:
        for key in ("offerAbsoluteUri", "offerUrl", "uri", "url"):
            val = raw.get(key, "")
            if val:
                if val.startswith("http"):
                    return val
                if val.startswith("/"):
                    return f"{OFFER_BASE}{val}"
                return f"{OFFER_BASE}/praca/{val}"

        # dehydratedState format: URL is inside nested "offers" list
        nested = raw.get("offers") or []
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict):
                    uri = o.get("offerAbsoluteUri") or o.get("offerUrl") or ""
                    if uri:
                        return uri if uri.startswith("http") else f"{OFFER_BASE}{uri}"

        # JSON-LD format
        if raw.get("@type") == "JobPosting" and raw.get("url"):
            return raw["url"]

        return ""

    @staticmethod
    def _parse_location(raw: dict) -> str:
        # __NEXT_DATA__ format
        places = raw.get("offers") or []
        if isinstance(places, list):
            cities = set()
            remote = False
            for place in places:
                if isinstance(place, dict):
                    city = (
                        place.get("displayWorkplace")
                        or place.get("city")
                        or place.get("label")
                        or ""
                    )
                    if city:
                        cities.add(city.strip())
                    if place.get("remoteWork") or place.get("remote"):
                        remote = True
            if cities:
                loc = ", ".join(sorted(cities))
                return f"{loc} (Remote)" if remote else loc
            if remote:
                return "Remote"

        # Simple location fields
        location = raw.get("location") or raw.get("displayWorkplace") or ""
        if isinstance(location, dict):
            location = location.get("label") or location.get("city") or ""
        location = str(location).strip()

        remote = raw.get("remoteWork") or raw.get("remote", False)
        if location and remote:
            return f"{location} (Remote)"
        if location:
            return location
        if remote:
            return "Remote"

        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        sal_text = raw.get("salaryDisplayText") or raw.get("salary") or ""
        if isinstance(sal_text, str) and sal_text.strip():
            return sal_text.strip()

        if isinstance(sal_text, dict):
            lo = sal_text.get("from") or sal_text.get("min")
            hi = sal_text.get("to") or sal_text.get("max")
            currency = sal_text.get("currency", "PLN")
            if lo or hi:
                return f"{lo or '?'}-{hi or '?'} {currency}"

        # Check nested offers for salary
        offers = raw.get("offers") or []
        if isinstance(offers, list):
            for o in offers:
                if not isinstance(o, dict):
                    continue
                sal = o.get("salaryDisplayText") or o.get("salary") or ""
                if isinstance(sal, str) and sal.strip():
                    return sal.strip()

        return None
