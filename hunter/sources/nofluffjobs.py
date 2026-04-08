"""
NoFluffJobs source.

API: POST https://nofluffjobs.com/api/search/posting
     ?salaryCurrency=PLN&salaryPeriod=month&region=pl
Body: {"criteriaSearch": {"category": ["frontend"]}, "page": 1}

Returns up to 265+ frontend jobs. We run two searches:
  1. All frontend jobs (global filter handles Wroclaw/Remote)
  2. Remote-only (extra coverage)

Tested 2026-04: endpoint works without auth.
"""

import logging
from typing import Optional

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://nofluffjobs.com/api/search/posting"
API_PARAMS = {
    "salaryCurrency": "PLN",
    "salaryPeriod": "month",
    "region": "pl",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://nofluffjobs.com",
    "Referer": "https://nofluffjobs.com/",
}
TIMEOUT = 20
JOB_BASE_URL = "https://nofluffjobs.com/pl/job"


class NoFluffJobsSource(BaseSource):
    name = "nofluffjobs"

    def search(self) -> list[Job]:
        all_jobs: list[Job] = []

        for body in self._search_bodies():
            try:
                all_jobs.extend(self._fetch(body))
            except Exception as e:
                logger.error(f"[NoFluffJobs] fetch error: {e}")

        # Deduplicate by (title + company): NoFluffJobs creates a separate
        # URL per region for the same job (e.g. polcode-remote, polcode-lodz).
        # We keep only the first occurrence — usually the remote/main one.
        seen: set[str] = set()
        jobs: list[Job] = []
        for job in all_jobs:
            key = f"{job.title.lower()}|{job.company.lower()}"
            if key not in seen:
                seen.add(key)
                jobs.append(job)

        logger.info(f"[NoFluffJobs] {len(all_jobs)} raw → {len(jobs)} after dedup by title+company")
        return jobs

    # ── Search request bodies ─────────────────────────────────────────────────

    def _search_bodies(self) -> list[dict]:
        """Two queries: all frontend + remote-only for better coverage."""
        return [
            {
                "criteriaSearch": {"category": ["frontend"]},
                "page": 1,
            },
            {
                "criteriaSearch": {"category": ["frontend"], "requirement": ["remote"]},
                "page": 1,
            },
        ]

    # ── HTTP fetch ────────────────────────────────────────────────────────────

    def _fetch(self, body: dict) -> list[Job]:
        resp = requests.post(
            API_URL,
            params=API_PARAMS,
            json=body,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        postings = data.get("postings", [])
        logger.info(f"[NoFluffJobs] fetched {len(postings)} raw postings")

        jobs = []
        for posting in postings:
            job = self._parse(posting)
            if job:
                jobs.append(job)
        return jobs

    # ── Parser ────────────────────────────────────────────────────────────────

    def _parse(self, p: dict) -> Optional[Job]:
        title = (p.get("title") or "").strip()
        company = (p.get("name") or "").strip()
        slug = (p.get("url") or "").strip()

        if not title or not company or not slug:
            return None

        url = f"{JOB_BASE_URL}/{slug}"
        location = self._parse_location(p)
        salary = self._parse_salary(p)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=p,
        )

    @staticmethod
    def _parse_location(p: dict) -> str:
        fully_remote = p.get("fullyRemote", False)
        places = (p.get("location") or {}).get("places", [])

        if fully_remote and not places:
            return "Remote"

        cities = [pl.get("city", "") for pl in places if pl.get("city")]
        city = cities[0] if cities else ""

        if fully_remote:
            return f"{city} (Remote)" if city else "Remote"
        if city:
            return city  # global filter checks for wroclaw/hybrid/remote in location
        return "Unknown"

    @staticmethod
    def _parse_salary(p: dict) -> Optional[str]:
        sal = p.get("salary")
        if not sal:
            return None

        low = sal.get("from")
        high = sal.get("to")
        currency = (sal.get("currency") or "PLN").upper()
        emp_type = (sal.get("type") or "").upper()

        if low or high:
            if low and high:
                amount = f"{int(low):,}–{int(high):,}".replace(",", " ")
            elif low:
                amount = f"{int(low):,}+".replace(",", " ")
            else:
                amount = f"до {int(high):,}".replace(",", " ")
            label = f"{amount} {currency}"
            if emp_type:
                label += f" {emp_type}"
            return label

        return None
