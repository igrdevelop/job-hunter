"""
theprotocol.it source — Polish IT job board (by Pracuj.pl creators).

Strategy (updated):
  1. Use cloudscraper to bypass Cloudflare.
  2. Try __NEXT_DATA__ JSON (Next.js SSR) — same approach as pracuj.py.
  3. Fall back to BeautifulSoup DOM parsing if no __NEXT_DATA__ found.
  4. Each listing URL is wrapped in try/except — failures log a warning
     and never block the rest of the hunt.

Listing URLs:
  https://theprotocol.it/filtry/frontend;sp/wroclaw;wp
  https://theprotocol.it/filtry/angular;sp/wroclaw;wp
  https://theprotocol.it/filtry/frontend;sp?remote=true
"""

import json
import logging
import re
from typing import Optional

import cloudscraper

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://theprotocol.it"

LISTING_URLS = [
    f"{BASE}/filtry/frontend;sp/wroclaw;wp",
    f"{BASE}/filtry/angular;sp?remote=true",
    f"{BASE}/filtry/frontend;sp?remote=true",
]

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()
# theprotocol.it rejects old browser UAs with an "unsupportedBrowser" page.
# Override to a modern Chrome UA while keeping cloudscraper's Cloudflare bypass.
_scraper.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
})


class TheProtocolSource(BaseSource):
    name = "theprotocol"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for listing_url in LISTING_URLS:
            try:
                parsed = self._fetch_listing(listing_url)
                logger.info(f"[theprotocol] {listing_url} -> {len(parsed)} raw jobs")
                for raw in parsed:
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    if not self._is_relevant(raw, job):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
            except Exception as e:
                logger.warning(f"[theprotocol] listing failed, skipping {listing_url}: {e}")

        logger.info(f"[theprotocol] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- Listing fetch ---------------------------------------------------------

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[theprotocol] HTTP fetch failed for {url}: {e}")
            return []

        # Strategy 1: __NEXT_DATA__ (Next.js SSR — same stack as Pracuj.pl)
        jobs = self._extract_next_data(resp.text)
        if jobs:
            logger.debug(f"[theprotocol] __NEXT_DATA__ gave {len(jobs)} items from {url}")
            return jobs

        # Strategy 2: BeautifulSoup DOM fallback
        jobs = self._extract_bs4(resp.text)
        if jobs:
            logger.debug(f"[theprotocol] BeautifulSoup gave {len(jobs)} items from {url}")
            return jobs

        logger.warning(
            f"[theprotocol] 0 jobs from {url} "
            f"(SPA/empty? HTML length={len(resp.text)})"
        )
        return []

    # -- __NEXT_DATA__ parsing -------------------------------------------------

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

        # theprotocol.it current format: pageProps.offersResponse.offers
        offers_resp = page_props.get("offersResponse")
        if isinstance(offers_resp, dict):
            for sub in ("offers", "items", "results"):
                sub_val = offers_resp.get(sub)
                if isinstance(sub_val, list) and sub_val:
                    return sub_val

        # Direct listing keys (legacy / fallback)
        for key in ("offers", "data", "items", "results", "postings", "jobs"):
            val = page_props.get(key)
            if isinstance(val, list) and val:
                return val
            if isinstance(val, dict):
                for sub in ("offers", "items", "results", "postings"):
                    sub_val = val.get(sub)
                    if isinstance(sub_val, list) and sub_val:
                        return sub_val

        # React Query dehydratedState cache (Pracuj.pl pattern)
        return self._extract_dehydrated(page_props)

    @staticmethod
    def _extract_dehydrated(page_props: dict) -> list[dict]:
        ds = page_props.get("dehydratedState", {})
        for query in ds.get("queries", []):
            qdata = query.get("state", {}).get("data")
            if not isinstance(qdata, dict):
                continue
            for key in ("offers", "items", "results", "postings", "jobs"):
                items = qdata.get(key)
                if isinstance(items, list) and items and isinstance(items[0], dict):
                    return items
        return []

    # -- BeautifulSoup fallback ------------------------------------------------

    def _extract_bs4(self, html: str) -> list[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("[theprotocol] beautifulsoup4 not installed")
            return []

        soup = BeautifulSoup(html, "html.parser")
        results = []
        offer_links = soup.find_all("a", href=re.compile(r"szczegoly/praca/.*,oferta,"))
        seen_hrefs: set[str] = set()

        for a_tag in offer_links:
            href = a_tag.get("href", "")
            if not href or href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            link_text = a_tag.get_text(separator="\n", strip=True)
            if not link_text or len(link_text) < 5:
                continue

            raw = self._extract_card_data(a_tag, link_text, href)
            if raw:
                results.append(raw)

        return results

    def _extract_card_data(self, a_tag, link_text: str, href: str) -> Optional[dict]:
        lines = [ln.strip() for ln in link_text.split("\n") if ln.strip()]
        if not lines:
            return None

        title = ""
        title_el = a_tag.find(["h2", "h3"])
        if title_el:
            title = title_el.get_text(strip=True)
        if not title and lines:
            title = lines[0]

        company = ""
        for line in lines:
            skip_prefixes = (
                "quick apply", "start asap", "new", "remote", "zdalna",
                "hybrydowa", "stacjonarna", "hybrid", "full office",
            )
            if line.lower().startswith(skip_prefixes):
                continue
            if line == title:
                continue
            if len(line) > 3 and not re.match(r"^\d", line) and line not in title:
                company = line
                break

        text_lower = link_text.lower()
        work_mode = ""
        if "remote" in text_lower or "zdalna" in text_lower:
            work_mode = "remote"
        elif "hybrydowa" in text_lower or "hybrid" in text_lower:
            work_mode = "hybrid"
        elif "stacjonarna" in text_lower or "full office" in text_lower:
            work_mode = "on-site"

        location = ""
        city_match = re.search(
            r"(Wrocław|Warszawa|Kraków|Gdańsk|Poznań|Łódź|Katowice|"
            r"Szczecin|Lublin|Bydgoszcz|Białystok|Toruń|Rzeszów|Kielce|Olsztyn)",
            link_text, re.I,
        )
        if city_match:
            location = city_match.group(1)
        if work_mode == "remote":
            location = f"{location} (Remote)" if location else "Remote"
        elif work_mode == "hybrid" and location:
            location = f"{location} (Hybrid)"

        salary = ""
        sal_match = re.search(
            r"(\d[\d\s.,]*k?\s*[-–]\s*\d[\d\s.,]*k?\s*(?:zł|PLN)(?:\s*\([^)]+\))?)",
            link_text,
        )
        if sal_match:
            salary = sal_match.group(1).strip()

        offer_url = href if href.startswith("http") else f"{BASE}{href}"
        offer_url = offer_url.split("?")[0]

        return {
            "title": title,
            "company": company,
            "location": location or "Unknown",
            "work_mode": work_mode,
            "salary": salary,
            "url": offer_url,
            "_text": link_text,
        }

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()

        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, title, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = raw.get("_text", "").lower()
        combined = title + " " + text
        return any(kw in combined for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        # Support current API format, legacy __NEXT_DATA__ field names, and BS4 fallback
        title = (
            raw.get("title") or raw.get("jobTitle") or raw.get("name") or ""
        ).strip()
        if not title:
            return None

        company_raw = (
            raw.get("employer") or raw.get("companyName") or raw.get("company") or "Unknown"
        )
        company = (
            company_raw.get("name", "Unknown")
            if isinstance(company_raw, dict)
            else str(company_raw)
        ).strip()

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
        # Current API format: offerUrlName slug → construct canonical URL
        slug = raw.get("offerUrlName", "")
        if slug:
            return f"{BASE}/szczegoly/praca/{slug}"

        for key in ("offerAbsoluteUri", "offerUrl", "url", "uri", "href"):
            val = raw.get(key, "")
            if val:
                if val.startswith("http"):
                    return val
                if val.startswith("/"):
                    return f"{BASE}{val}"
        return ""

    @staticmethod
    def _parse_location(raw: dict) -> str:
        # Current API format: workplace=[{city, location, region}], workModes=["zdalna",...]
        work_modes = raw.get("workModes") or []
        is_remote = any(m in ("zdalna", "remote") for m in work_modes)
        is_hybrid = any(m in ("hybrydowa", "hybrid") for m in work_modes)

        city = ""
        workplace = raw.get("workplace")
        if isinstance(workplace, list) and workplace:
            first = workplace[0]
            city = (first.get("city") or first.get("location") or "").strip()
        elif isinstance(workplace, str):
            city = workplace.strip()

        # BS4 fallback: flat 'location' string
        if not city:
            loc = raw.get("location") or raw.get("displayWorkplace") or ""
            if isinstance(loc, dict):
                loc = loc.get("label") or loc.get("city") or ""
            city = str(loc).strip()

        # Legacy fields
        if not city:
            city = (raw.get("city") or "").strip()
        if not is_remote:
            is_remote = bool(raw.get("remoteWork") or raw.get("fullyRemote") or raw.get("remote"))

        if city and is_remote:
            return f"{city} (Remote)"
        if city and is_hybrid:
            return f"{city} (Hybrid)"
        if city:
            return city
        if is_remote:
            return "Remote"
        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        # Current API: typesOfContracts=[{id, salary: {from, to, currency, type}}]
        contracts = raw.get("typesOfContracts") or []
        for contract in contracts:
            if not isinstance(contract, dict):
                continue
            sal = contract.get("salary")
            if isinstance(sal, dict):
                lo = sal.get("from") or sal.get("min")
                hi = sal.get("to") or sal.get("max")
                currency = sal.get("currency", "PLN")
                if lo or hi:
                    return f"{lo or '?'}-{hi or '?'} {currency}"

        # Legacy / fallback fields
        sal = raw.get("salaryDisplayText") or raw.get("salary") or raw.get("salaryText") or ""
        if isinstance(sal, str) and sal.strip():
            return sal.strip()
        if isinstance(sal, dict):
            lo = sal.get("from") or sal.get("min")
            hi = sal.get("to") or sal.get("max")
            currency = sal.get("currency", "PLN")
            if lo or hi:
                return f"{lo or '?'}-{hi or '?'} {currency}"
        return None
