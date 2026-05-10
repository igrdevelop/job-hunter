"""
Recruitee public offers API (no auth):
  GET https://{slug}.recruitee.com/api/offers/
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from hunter.ats.base import ATSProvider
from hunter.models import Job

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://{slug}.recruitee.com/api/offers/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 30


class RecruiteeProvider(ATSProvider):
    name = "recruitee"

    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        url = API_TEMPLATE.format(slug=slug)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data: Any = resp.json()
        except Exception as e:
            logger.warning(f"[ats:recruitee:{slug}] fetch failed: {e}")
            return []

        if not isinstance(data, dict):
            return []
        if data.get("error"):
            logger.warning(f"[ats:recruitee:{slug}] API error: {data!r}")
            return []

        offers = data.get("offers")
        if not isinstance(offers, list):
            return []

        default_name = company_name or _slug_to_name(slug)
        jobs: list[Job] = []
        for raw in offers:
            if not isinstance(raw, dict):
                continue
            job = parse_recruitee_job(raw, slug, default_name)
            if job:
                jobs.append(job)

        logger.info(f"[ats:recruitee:{slug}] {len(jobs)} jobs")
        return jobs


def parse_recruitee_job(raw: dict, slug: str, company_name: str) -> Optional[Job]:
    title = (raw.get("title") or "").strip()
    if not title:
        return None

    job_url = (raw.get("careers_url") or "").strip()
    if not job_url:
        return None

    loc = (raw.get("location") or "").strip()
    if not loc:
        loc = (raw.get("country_code") or "").strip() or "Remote"
    if raw.get("remote_recruitment") is True and loc and "remote" not in loc.lower():
        if "(Remote)" not in loc:
            loc = f"{loc} (Remote)"

    return Job(
        title=title,
        company=company_name,
        location=loc,
        salary=None,
        url=job_url,
        source=f"ats:recruitee:{slug}",
        raw=raw,
    )


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug
