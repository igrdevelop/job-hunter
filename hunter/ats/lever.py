"""
Lever public postings API (no auth):
  GET https://api.lever.co/v0/postings/{slug}?mode=json
→ list[dict]  or  {"ok": false, "error": "..."} on failure
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from hunter.ats.base import ATSProvider
from hunter.models import Job

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://api.lever.co/v0/postings/{slug}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 30


class LeverProvider(ATSProvider):
    name = "lever"

    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        try:
            resp = requests.get(
                API_TEMPLATE.format(slug=slug),
                params={"mode": "json"},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data: Any = resp.json()
        except Exception as e:
            logger.warning(f"[ats:lever:{slug}] fetch failed: {e}")
            return []

        if isinstance(data, dict) and data.get("ok") is False:
            logger.warning(f"[ats:lever:{slug}] API error: {data.get('error')!r}")
            return []
        if not isinstance(data, list):
            return []

        default_name = company_name or _slug_to_name(slug)
        jobs: list[Job] = []
        for raw in data:
            if not isinstance(raw, dict):
                continue
            job = parse_lever_job(raw, slug, default_name)
            if job:
                jobs.append(job)

        logger.info(f"[ats:lever:{slug}] {len(jobs)} jobs")
        return jobs


def parse_lever_job(raw: dict, slug: str, company_name: str) -> Optional[Job]:
    title = (raw.get("text") or "").strip()
    if not title:
        return None

    job_url = (raw.get("hostedUrl") or "").strip()
    if not job_url:
        return None

    cats = raw.get("categories")
    if not isinstance(cats, dict):
        cats = {}
    location = (cats.get("location") or "").strip() or "Remote"

    return Job(
        title=title,
        company=company_name,
        location=location,
        salary=None,
        url=job_url,
        source=f"ats:lever:{slug}",
        raw=raw,
    )


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug
