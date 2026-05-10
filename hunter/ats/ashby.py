"""
Ashby public job board API (no auth):
  GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import requests

from hunter.ats.base import ATSProvider
from hunter.models import Job

logger = logging.getLogger(__name__)

API_TEMPLATE = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}
TIMEOUT = 30


class AshbyProvider(ATSProvider):
    name = "ashby"

    def fetch(self, slug: str, company_name: Optional[str] = None) -> list[Job]:
        url = API_TEMPLATE.format(slug=slug)
        try:
            resp = requests.get(
                url,
                params={"includeCompensation": "true"},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data: Any = resp.json()
        except Exception as e:
            logger.warning(f"[ats:ashby:{slug}] fetch failed: {e}")
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
            job = parse_ashby_job(raw, slug, default_name)
            if job:
                jobs.append(job)

        logger.info(f"[ats:ashby:{slug}] {len(jobs)} jobs")
        return jobs


def parse_ashby_job(raw: dict, slug: str, company_name: str) -> Optional[Job]:
    if raw.get("isListed") is False:
        return None

    title = (raw.get("title") or "").strip()
    if not title:
        return None

    job_url = (raw.get("jobUrl") or "").strip()
    if not job_url:
        return None

    location = _ashby_location_string(raw)
    salary = _extract_ashby_salary(raw.get("compensation"))

    return Job(
        title=title,
        company=company_name,
        location=location,
        salary=salary,
        url=job_url,
        source=f"ats:ashby:{slug}",
        raw=raw,
    )


def _ashby_location_string(raw: dict) -> str:
    loc: Any = raw.get("locationName") or raw.get("location")
    if isinstance(loc, dict):
        text = (loc.get("name") or loc.get("city") or "").strip() or "Unknown"
    elif isinstance(loc, str):
        text = loc.strip() or "Unknown"
    else:
        text = "Unknown"
    if raw.get("isRemote") is True and "remote" not in text.lower():
        if text in ("", "Unknown"):
            return "Remote"
        return f"{text} (Remote)"
    return text


def _extract_ashby_salary(comp: Any) -> Optional[str]:
    if not comp or not isinstance(comp, dict):
        return None
    summary = comp.get("scrapeableCompensationSalarySummary") or comp.get("compensationTierSummary")
    if isinstance(summary, str) and summary.strip():
        return summary.strip()
    parts = comp.get("summaryComponents")
    if isinstance(parts, list) and parts:
        texts = []
        for p in parts:
            if isinstance(p, dict) and p.get("title"):
                texts.append(str(p["title"]).strip())
            elif isinstance(p, str) and p.strip():
                texts.append(p.strip())
        if texts:
            return " | ".join(texts)
    tiers = comp.get("compensationTiers")
    if isinstance(tiers, list) and tiers:
        t0 = tiers[0]
        if isinstance(t0, dict):
            interval = t0.get("interval") or t0
            if isinstance(interval, dict):
                cur = (interval.get("currencyCode") or interval.get("currency") or "").strip()
                min_v = interval.get("minValue")
                max_v = interval.get("maxValue")
                if min_v is not None and max_v is not None:
                    return f"{min_v}–{max_v} {cur}".strip() if cur else f"{min_v}–{max_v}"
    return None


def _slug_to_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug
