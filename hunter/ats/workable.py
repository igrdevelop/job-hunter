"""
Workable ATS adapter.

Public widget API used by Workable's "Advanced Career Pages" (no auth):
  GET https://apply.workable.com/api/v1/widget/accounts/{slug}
  → { "name", "description", "jobs": [ { "title", "shortcode", "state",
       "country", "city", "telecommuting", "url", "shortlink",
       "application_url", "published_on", "department", ... } ] }

Job URL format (taken straight from the response): https://apply.workable.com/j/{shortcode}
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from hunter.ats.base import ATSProvider
from hunter.models import Job

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://apply.workable.com/api/v1/widget/accounts/{slug}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://apply.workable.com/",
}
TIMEOUT = 30


class WorkableProvider(ATSProvider):
    name = "workable"

    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        url = API_TEMPLATE.format(slug=slug)
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[ats:workable:{slug}] fetch failed: {e}")
            return []

        results = _extract_results(data)
        display_name = company_name or _slug_to_name(slug)
        jobs: list[Job] = []
        for raw in results:
            job = parse_workable_job(raw, slug, display_name)
            if job:
                jobs.append(job)

        logger.info(f"[ats:workable:{slug}] {len(jobs)} jobs")
        return jobs


def parse_workable_job(raw: dict, slug: str, company_name: str) -> Optional[Job]:
    """Pure parser: dict → Job. Returns None for archived/draft/incomplete entries."""
    if not isinstance(raw, dict):
        return None

    title = (raw.get("title") or "").strip()
    if not title:
        return None

    state = (raw.get("state") or "").strip().lower()
    if state and state not in ("published", "open"):
        return None

    shortcode = (raw.get("shortcode") or "").strip()
    job_url = (raw.get("url") or raw.get("shortlink") or raw.get("application_url") or "").strip()
    if not job_url and shortcode:
        job_url = f"https://apply.workable.com/j/{shortcode}"
    if not job_url:
        return None

    return Job(
        title=title,
        company=company_name,
        location=_format_location(raw),
        salary=None,
        url=job_url,
        source=f"ats:workable:{slug}",
        raw=raw,
    )


def _extract_results(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    for key in ("results", "jobs", "data"):
        val = data.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
    return []


def _format_location(raw: dict) -> str:
    """Build a location string the central filter understands.

    Workable returns flat country/city plus telecommuting/remote flags.
    """
    location_obj = raw.get("location") if isinstance(raw.get("location"), dict) else {}
    city = (raw.get("city") or location_obj.get("city") or "").strip()
    country = (
        raw.get("country")
        or raw.get("countryCode")
        or location_obj.get("country")
        or location_obj.get("countryCode")
        or ""
    ).strip()
    is_remote = bool(
        raw.get("telecommuting")
        or raw.get("remote")
        or location_obj.get("telecommuting")
    )

    parts = [p for p in (city, country) if p]
    base = ", ".join(parts) if parts else ""

    if is_remote:
        return f"{base} (Remote)" if base else "Remote"
    return base or "Unknown"


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug
