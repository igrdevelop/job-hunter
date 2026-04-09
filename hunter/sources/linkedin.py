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
from typing import Optional

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


class LinkedInSource(BaseSource):
    name = "linkedin"

    def search(self) -> list[Job]:
        keywords_raw = os.environ.get("LINKEDIN_KEYWORDS", "angular,angular developer,frontend angular")
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
            "f_E": "3,4",       # mid + senior
            "sortBy": "DD",     # most recent
            "start": str(start),
        }

        resp = requests.get(SEARCH_API, params=params, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code != 200:
            logger.error(f"[LinkedIn] API returned {resp.status_code}")
            return []

        return self._parse_html(resp.text)

    def _parse_html(self, html: str) -> list[Job]:
        """Parse HTML fragments from the guest search API."""
        titles = re.findall(
            r'<h3[^>]*base-search-card__title[^>]*>\s*(.*?)\s*</h3>', html, re.S
        )
        companies = re.findall(
            r'<h4[^>]*base-search-card__subtitle[^>]*>\s*<a[^>]*>\s*(.*?)\s*</a>',
            html, re.S,
        )
        locations = re.findall(
            r'<span[^>]*job-search-card__location[^>]*>\s*(.*?)\s*</span>', html, re.S
        )
        job_ids = re.findall(
            r'data-entity-urn="urn:li:jobPosting:(\d+)"', html
        )

        jobs: list[Job] = []
        for i in range(len(job_ids)):
            title = titles[i].strip() if i < len(titles) else ""
            company = companies[i].strip() if i < len(companies) else "Unknown"
            location = locations[i].strip() if i < len(locations) else "Unknown"
            job_id = job_ids[i]

            if not title:
                continue

            # Strip company name suffix: "Senior Dev / VBET" -> "Senior Dev"
            if company:
                title = re.sub(
                    r'\s*[-/|]\s*' + re.escape(company.strip()) + r'\s*$',
                    '', title, flags=re.I,
                ).strip()

            jobs.append(Job(
                title=title,
                company=company,
                location=location,
                salary=None,
                url=f"https://www.linkedin.com/jobs/view/{job_id}/",
                source=self.name,
                raw={"jobId": job_id},
            ))

        return jobs

    @staticmethod
    def _extract_job_id(url: str) -> Optional[str]:
        m = re.search(r"/jobs/view/(\d+)", url)
        return m.group(1) if m else None
