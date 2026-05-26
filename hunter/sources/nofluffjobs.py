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
import re
from typing import Optional
from urllib.parse import urlparse

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

POSTING_API = "https://nofluffjobs.com/api/posting"
POSTING_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://nofluffjobs.com/",
}


def _extract_posting_slug(url: str) -> str:
    match = re.search(r"/job/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Cannot extract NoFluffJobs slug from URL: {url}")
    return match.group(1)


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _format_posting_text(data: dict) -> str:
    parts: list[str] = [
        f"Job Title: {data.get('title', 'N/A')}",
        f"Company: {data.get('name', 'N/A')}",
    ]

    location = data.get("location", {})
    places = location.get("places", [])
    remote = data.get("fullyRemote", False)
    loc_str = "Remote" if remote else ", ".join(p.get("city", "") for p in places)
    parts.append(f"Location: {loc_str}")

    seniority = data.get("seniority", [])
    if seniority:
        parts.append(f"Seniority: {', '.join(seniority)}")

    musts = data.get("requirements", {}).get("musts", [])
    nices = data.get("requirements", {}).get("nices", [])
    if musts:
        parts.append(f"Must-have: {', '.join(m.get('value', '') for m in musts)}")
    if nices:
        parts.append(f"Nice-to-have: {', '.join(n.get('value', '') for n in nices)}")

    salary = data.get("essentials", {}).get("salary", {})
    if salary:
        low = salary.get("from")
        high = salary.get("to")
        cur = salary.get("currency", "PLN")
        emp = salary.get("type", "")
        if low or high:
            parts.append(f"Salary: {low or '?'}–{high or '?'} {cur} {emp}")

    sections = data.get("sections", {})
    for key in ("requirements", "responsibilities", "description", "methodology", "environment"):
        content = sections.get(key, "")
        if content:
            parts.append(f"\n--- {key.title()} ---\n{_strip_html(content)}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError("NoFluffJobs posting returned almost no content")
    return text


class NoFluffJobsSource(BaseSource):
    name = "nofluffjobs"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "nofluffjobs.com" in host

    def fetch_text(self, url: str) -> str:
        """Try the posting detail API first, fall back to generic HTML extraction."""
        slug = _extract_posting_slug(url)
        try:
            resp = requests.get(
                f"{POSTING_API}/{slug}", headers=POSTING_HEADERS, timeout=TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()
            return _format_posting_text(data)
        except Exception:
            from hunter.sources.html_fallback import fetch_html
            return fetch_html(url)

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
                amount = f"up to {int(high):,}".replace(",", " ")
            label = f"{amount} {currency}"
            if emp_type:
                label += f" {emp_type}"
            return label

        return None
