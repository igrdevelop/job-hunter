"""
theprotocol.it source — Polish IT job board (by Pracuj.pl creators).

Strategy:
  1. Fetch listing pages for frontend/angular jobs in Wroclaw + remote.
     Pages are server-rendered HTML with job cards as <a> links.
  2. Parse job data from the listing HTML using BeautifulSoup.
  3. Individual job text is fetched lazily by job_fetch/theprotocol.py
     when LLM processing is triggered.

Listing URLs:
  https://theprotocol.it/filtry/frontend;sp/wroclaw;wp
  https://theprotocol.it/filtry/angular;sp/wroclaw;wp
  https://theprotocol.it/filtry/frontend;sp?remote=true
"""

import logging
import re
from typing import Optional

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://theprotocol.it"

LISTING_URLS = [
    f"{BASE}/filtry/frontend;sp/wroclaw;wp",
    f"{BASE}/filtry/angular;sp/wroclaw;wp",
    f"{BASE}/filtry/frontend;sp?remote=true",
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


class TheProtocolSource(BaseSource):
    name = "theprotocol"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            parsed = self._fetch_listing(url)
            logger.info(f"[theprotocol] {url} -> {len(parsed)} raw jobs")
            for raw in parsed:
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                if not self._is_relevant(raw, job):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[theprotocol] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- Listing fetch ---------------------------------------------------------

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[theprotocol] fetch listing {url}: {e}")
            return []

        return self._parse_listing_html(resp.text)

    def _parse_listing_html(self, html: str) -> list[dict]:
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
        """Extract structured data from a job card <a> tag."""
        lines = [l.strip() for l in link_text.split("\n") if l.strip()]
        if not lines:
            return None

        # Title is typically in an <h2> or <h3> inside the link
        title = ""
        title_el = a_tag.find(["h2", "h3"])
        if title_el:
            title = title_el.get_text(strip=True)
        if not title and lines:
            title = lines[0]

        # Company - usually the text line after common labels
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
            # Likely a company name if it contains uppercase and isn't a tech tag
            if len(line) > 3 and not re.match(r"^\d", line) and line not in title:
                company = line
                break

        # Work mode and location from text
        text_lower = link_text.lower()
        work_mode = ""
        if "remote" in text_lower or "zdalna" in text_lower:
            work_mode = "remote"
        elif "hybrydowa" in text_lower or "hybrid" in text_lower:
            work_mode = "hybrid"
        elif "stacjonarna" in text_lower or "full office" in text_lower:
            work_mode = "on-site"

        # Location - look for Polish city names
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

        # Salary - look for patterns like "23k-32.5k" or "110-130zł"
        salary = ""
        sal_match = re.search(
            r"(\d[\d\s.,]*k?\s*[-–]\s*\d[\d\s.,]*k?\s*(?:zł|PLN)(?:\s*\([^)]+\))?)",
            link_text,
        )
        if sal_match:
            salary = sal_match.group(1).strip()

        # Technologies - short words that look like tech tags
        techs = []
        known_techs = {
            "angular", "react", "vue", "typescript", "javascript", "html", "css",
            "scss", "sass", "rxjs", "ngrx", "node.js", "next.js", "bootstrap",
            "tailwind", "jest", "cypress", "git", "docker", "webpack", "vite",
            "redux", "graphql", "postgresql", "mongodb", "aws", "azure",
            "storybook", "figma", "jira", "confluence", ".net", "java", "python",
            "react.js", "vue.js", "angular.js", "ag grid",
        }
        for line in lines:
            if line.lower() in known_techs:
                techs.append(line)

        # Build URL
        offer_url = href if href.startswith("http") else f"{BASE}{href}"
        # Strip query params (searchId etc.)
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

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )
