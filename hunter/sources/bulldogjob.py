"""
Bulldogjob.com source.

Strategy (tested 2026-04):
  1. Fetch listing page for Angular mid/senior jobs via HTTPS.
     The page contains __NEXT_DATA__ JSON with up to 21+ job objects.
  2. Parse jobs directly from pageProps.jobs — no extra detail calls needed
     for filtering; Job objects are built from listing-level data.
  3. Individual job description text is fetched lazily by job_fetch/bulldogjob.py
     only when LLM processing is triggered.

Listing URL: https://bulldogjob.com/companies/jobs/s/skills,Angular/experience,mid,senior
Job page URL: https://bulldogjob.com/companies/jobs/{job_id}
"""

import json
import logging
import re
from typing import Optional

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://bulldogjob.com"

LISTING_URLS = [
    f"{BASE}/companies/jobs/s/skills,Angular/experience,mid,senior",
    f"{BASE}/companies/jobs/s/skills,Angular/experience,mid,senior/remote,true",
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

TIMEOUT = 20


class BulldogJobSource(BaseSource):
    name = "bulldogjob"

    def search(self) -> list[Job]:
        seen_ids: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            raw_jobs = self._fetch_listing(url)
            logger.info(f"[Bulldogjob] {url} → {len(raw_jobs)} raw jobs")
            for raw in raw_jobs:
                job_id = raw.get("id", "")
                if not job_id or job_id in seen_ids:
                    continue
                if not self._is_relevant(raw):
                    continue
                seen_ids.add(job_id)
                job = self._parse(raw)
                if job:
                    jobs.append(job)

        logger.info(f"[Bulldogjob] {len(jobs)} jobs after pre-filter")
        return jobs

    # ── Listing fetch ─────────────────────────────────────────────────────────

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            m = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
                resp.text,
                re.S,
            )
            if not m:
                logger.warning(f"[Bulldogjob] No __NEXT_DATA__ on {url}")
                return []
            data = json.loads(m.group(1))
            jobs = (
                data.get("props", {})
                .get("pageProps", {})
                .get("jobs", [])
            )
            return jobs if isinstance(jobs, list) else []
        except Exception as e:
            logger.error(f"[Bulldogjob] fetch listing {url}: {e}")
            return []

    # ── Pre-filter (before building Job object) ───────────────────────────────

    def _is_relevant(self, raw: dict) -> bool:
        """Quick keyword pre-filter on title + tags."""
        title = (raw.get("position") or "").lower()
        tags = [t.lower() for t in (raw.get("technologyTags") or [])]
        level = (raw.get("experienceLevel") or "").lower()
        main_tech = (raw.get("mainTechnology") or "").lower()

        # Exclude juniors / interns
        exclude_levels = [lv.lower() for lv in FILTER.get("exclude_levels", [])]
        if level in exclude_levels:
            return False

        combined = title + " " + " ".join(tags) + " " + main_tech

        if FILTER.get("require_angular", False):
            return "angular" in combined

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        return any(kw in combined for kw in keywords)

    # ── Parser ────────────────────────────────────────────────────────────────

    def _parse(self, raw: dict) -> Optional[Job]:
        job_id = (raw.get("id") or "").strip()
        title = (raw.get("position") or "").strip()
        company_data = raw.get("company") or {}
        company = (company_data.get("name") or "").strip()

        if not job_id or not title or not company:
            return None

        url = f"{BASE}/companies/jobs/{job_id}"
        location = self._parse_location(raw)
        salary = self._parse_salary(raw)

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
    def _parse_location(raw: dict) -> str:
        is_remote = raw.get("remote", False)
        city = (raw.get("city") or "").strip()

        if is_remote and not city:
            return "Remote"
        if is_remote and city:
            return f"{city} (Remote)"
        if city:
            return city
        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        sal = raw.get("denominatedSalaryLong") or {}
        money = sal.get("money")
        currency = (sal.get("currency") or "PLN").upper()

        if money:
            return f"{money} {currency}"
        return None
