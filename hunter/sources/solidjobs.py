"""
solid.jobs source — Polish developer-focused IT job board.

Strategy:
  1. Fetch listing pages for frontend/angular jobs in Wroclaw + remote.
     Uses matrix-style URL parameters (;keywords=...;cities=...).
     Pages are server-rendered HTML with job cards.
  2. Parse job data from the listing HTML using BeautifulSoup.
  3. Individual job text is fetched lazily by job_fetch/solidjobs.py
     when LLM processing is triggered.

Listing URLs:
  https://solid.jobs/offers/it;keywords=angular;cities=Wrocław
  https://solid.jobs/offers/it;keywords=angular
  https://solid.jobs/offers/it;keywords=frontend;cities=Wrocław
"""

import logging
import re
from typing import Optional

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://solid.jobs"

LISTING_URLS = [
    f"{BASE}/offers/it;keywords=angular;cities=Wrocław",
    f"{BASE}/offers/it;keywords=angular",
    f"{BASE}/offers/it;keywords=frontend;cities=Wrocław",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": f"{BASE}/",
}
TIMEOUT = 25


class SolidJobsSource(BaseSource):
    name = "solidjobs"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            parsed = self._fetch_listing(url)
            logger.info(f"[solidjobs] {url} -> {len(parsed)} raw jobs")
            for raw in parsed:
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                if not self._is_relevant(raw, job):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[solidjobs] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- Listing fetch ---------------------------------------------------------

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[solidjobs] fetch listing {url}: {e}")
            return []

        return self._parse_listing_html(resp.text)

    def _parse_listing_html(self, html: str) -> list[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            logger.error("[solidjobs] beautifulsoup4 not installed")
            return []

        soup = BeautifulSoup(html, "html.parser")
        results = []

        offer_links = soup.find_all("a", href=re.compile(r"/offer/\d+/"))
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
        """Extract structured data from a job card."""
        lines = [l.strip() for l in link_text.split("\n") if l.strip()]
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
            if line == title:
                continue
            if line.startswith("#"):
                continue
            skip_words = (
                "remote", "zdalnie", "hybrid", "hybrydowa",
                "stacjonarna", "on-site", "new", "nowe",
            )
            if line.lower() in skip_words:
                continue
            if len(line) > 2 and not re.match(r"^[\d.,\s]+$", line):
                company = line
                break

        text_lower = link_text.lower()
        work_mode = ""
        if "remote" in text_lower or "zdalnie" in text_lower or "100% remote" in text_lower:
            work_mode = "remote"
        elif "hybrid" in text_lower or "hybrydowa" in text_lower:
            work_mode = "hybrid"
        elif "stacjonarna" in text_lower or "on-site" in text_lower:
            work_mode = "on-site"

        location = ""
        city_pattern = re.compile(
            r"(Wrocław|Warszawa|Kraków|Gdańsk|Poznań|Łódź|Katowice|"
            r"Szczecin|Lublin|Bydgoszcz|Białystok|Toruń|Rzeszów|Kielce|Olsztyn)",
            re.I,
        )
        city_match = city_pattern.search(link_text)
        if city_match:
            location = city_match.group(1)
        if work_mode == "remote":
            location = f"{location} (Remote)" if location else "Remote"
        elif work_mode == "hybrid" and location:
            location = f"{location} (Hybrid)"

        salary = ""
        sal_match = re.search(
            r"(\d[\d\s.,]*\s*[-–]\s*\d[\d\s.,]*\s*(?:zł|PLN|EUR|USD)(?:\s*(?:net|gross|netto|brutto))?(?:\s*\([^)]+\))?)",
            link_text, re.I,
        )
        if sal_match:
            salary = sal_match.group(1).strip()

        # Tech tags on solid.jobs appear as #Technology tokens
        techs = []
        tech_matches = re.findall(r"#([A-Za-z0-9.+]+)", link_text)
        for t in tech_matches:
            techs.append(t)

        offer_url = href if href.startswith("http") else f"{BASE}{href}"
        offer_url = offer_url.split("?")[0]

        return {
            "title": title,
            "company": company,
            "location": location or "Unknown",
            "work_mode": work_mode,
            "salary": salary,
            "techs": techs,
            "url": offer_url,
            "_text": link_text,
        }

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()

        exclude_patterns = FILTER.get("exclude_patterns", [])
        for pat in exclude_patterns:
            if re.search(pat, title, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = raw.get("_text", "").lower()
        combined = title + " " + text
        return any(kw in combined for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        if not title:
            return None

        company = (raw.get("company") or "Unknown").strip()
        location = (raw.get("location") or "Unknown").strip()
        salary = raw.get("salary") or None
        url = raw.get("url", "")

        if not url:
            return None

        techs = raw.get("techs", [])
        raw_data = dict(raw)
        if techs:
            raw_data["technology"] = [{"name": t} for t in techs]

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw_data,
        )
