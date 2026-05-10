"""
Greenhouse ATS public API (no auth):
  GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs
"""

from __future__ import annotations

import logging
from typing import Optional

import requests

from hunter.ats.base import ATSProvider
from hunter.models import Job

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 30


class GreenhouseProvider(ATSProvider):
    name = "greenhouse"

    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        url = API_TEMPLATE.format(slug=slug)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[ats:greenhouse:{slug}] fetch failed: {e}")
            return []

        if not isinstance(data, dict):
            return []

        results = data.get("jobs")
        if not isinstance(results, list):
            return []

        default_name = company_name or _slug_to_name(slug)
        jobs: list[Job] = []
        for raw in results:
            if not isinstance(raw, dict):
                continue
            job = parse_greenhouse_job(raw, slug, default_name)
            if job:
                jobs.append(job)

        logger.info(f"[ats:greenhouse:{slug}] {len(jobs)} jobs")
        return jobs


def parse_greenhouse_job(raw: dict, slug: str, company_name: str) -> Optional[Job]:
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    job_url = (raw.get("absolute_url") or "").strip()
    if not job_url:
        return None

    from_api = (raw.get("company_name") or "").strip()
    comp = from_api or company_name

    loc = raw.get("location")
    if isinstance(loc, dict):
        loc_name = (loc.get("name") or "").strip()
    elif isinstance(loc, str):
        loc_name = loc.strip()
    else:
        loc_name = ""
    location = loc_name or "Unknown"

    return Job(
        title=title,
        company=comp,
        location=location,
        salary=None,
        url=job_url,
        source=f"ats:greenhouse:{slug}",
        raw=raw,
    )


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug
