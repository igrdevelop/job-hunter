"""
4dayweek.io — public JSON API v2 (no auth).

API: GET https://4dayweek.io/api/v2/jobs
Docs: https://4dayweek.io/developers
OpenAPI: https://4dayweek.io/openapi.yaml

Salary fields in API responses are in smallest currency units (e.g. cents); divide by 100 for display.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_LIST_URL = "https://4dayweek.io/api/v2/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://4dayweek.io/",
}
TIMEOUT = 45
PAGE_LIMIT = 100
MAX_PAGES_PER_QUERY = 8
REQUEST_DELAY_SEC = 0.45
DEFAULT_RATE_LIMIT_WAIT_SEC = 60

# Complementary full-text queries; merged and deduped by job URL.
SEARCH_QUERIES: tuple[str, ...] = ("frontend", "typescript", "angular")


class FourdayweekSource(BaseSource):
    name = "fourdayweek"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for q in SEARCH_QUERIES:
            page = 1
            while page <= MAX_PAGES_PER_QUERY:
                if page > 1:
                    time.sleep(REQUEST_DELAY_SEC)
                payload = self._fetch_list({"q": q, "page": page, "limit": PAGE_LIMIT})
                if not payload:
                    break
                batch = payload.get("data")
                if not isinstance(batch, list) or not batch:
                    break
                for raw in batch:
                    if not isinstance(raw, dict):
                        continue
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    ctx = _prefilter_context(raw)
                    if not self.matches_coarse_prefilter(job.title, ctx):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
                logger.info(
                    f"[4dayweek] q={q!r} page={page} -> +{len(batch)} raw "
                    f"(unique total {len(jobs)})"
                )
                if not payload.get("has_more"):
                    break
                page += 1

        logger.info(f"[4dayweek] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_list(self, params: dict[str, Any]) -> Optional[dict[str, Any]]:
        for _ in range(3):
            try:
                resp = requests.get(
                    API_LIST_URL,
                    params=params,
                    headers=HEADERS,
                    timeout=TIMEOUT,
                )
                if resp.status_code == 429:
                    wait = _retry_after_seconds(resp)
                    logger.warning(f"[4dayweek] rate limited (429), waiting {wait}s")
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    return None
                return data
            except Exception as e:
                logger.warning(f"[4dayweek] list fetch failed {params}: {e}")
                return None
        return None

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        company_obj = raw.get("company")
        company = ""
        if isinstance(company_obj, dict):
            company = (company_obj.get("name") or "").strip()
        if not title or not url or not company:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw),
            salary=_format_salary(raw),
            url=url,
            source=self.name,
            raw=raw,
        )


def _retry_after_seconds(resp: requests.Response) -> int:
    raw = resp.headers.get("Retry-After")
    if raw is not None:
        try:
            return max(1, int(float(raw)))
        except (TypeError, ValueError):
            pass
    return DEFAULT_RATE_LIMIT_WAIT_SEC


def _format_location(raw: dict) -> str:
    arrangement = (raw.get("work_arrangement") or "").strip().lower()
    remote = raw.get("is_remote") is True
    parts: list[str] = []
    if arrangement:
        parts.append(arrangement.capitalize())

    offices = raw.get("office_locations")
    if isinstance(offices, list) and offices:
        loc_bits: list[str] = []
        for o in offices[:5]:
            if not isinstance(o, dict):
                continue
            city = (o.get("city") or "").strip()
            country = (o.get("country") or "").strip()
            if city and country:
                loc_bits.append(f"{city}, {country}")
            elif country:
                loc_bits.append(country)
            elif city:
                loc_bits.append(city)
        if loc_bits:
            parts.append("Offices: " + "; ".join(loc_bits))

    allowed = raw.get("remote_allowed")
    if isinstance(allowed, list) and allowed:
        countries: list[str] = []
        for a in allowed[:12]:
            if isinstance(a, dict):
                c = (a.get("country") or "").strip()
                if c:
                    countries.append(c)
        if countries:
            parts.append("Remote: " + ", ".join(countries))

    if remote and not parts:
        return "Remote"
    return " | ".join(parts) if parts else "Unknown"


def _minor_to_major(amount: Optional[int]) -> Optional[int]:
    if amount is None:
        return None
    try:
        return int(amount) // 100
    except (TypeError, ValueError):
        return None


def _format_salary(raw: dict) -> Optional[str]:
    lo_m = _minor_to_major(raw.get("salary_min"))
    hi_m = _minor_to_major(raw.get("salary_max"))
    cur = (raw.get("salary_currency") or "USD").strip()
    period = (raw.get("salary_period") or "year").strip().lower()

    if (lo_m is None or lo_m <= 0) and (hi_m is None or hi_m <= 0):
        return None

    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    period_suffix = {"year": "/yr", "month": "/mo", "hour": "/hr"}.get(period, f"/{period}")

    lo = lo_m or 0
    hi = hi_m or 0
    if lo and hi:
        return f"{fmt(lo)}–{fmt(hi)} {cur}{period_suffix}"
    if lo:
        return f"{fmt(lo)}+ {cur}{period_suffix}"
    return f"up to {fmt(hi)} {cur}{period_suffix}"


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    desc = raw.get("description")
    if isinstance(desc, str) and desc.strip():
        parts.append(desc.strip()[:1200])
    for key in ("skills", "stack", "tools"):
        val = raw.get(key)
        if isinstance(val, list):
            names: list[str] = []
            for item in val:
                if isinstance(item, dict):
                    n = (item.get("name") or "").strip()
                    if n:
                        names.append(n)
            if names:
                parts.append(" ".join(names))
    role = raw.get("role")
    if isinstance(role, str) and role.strip():
        parts.append(role.strip())
    cat = raw.get("category")
    if isinstance(cat, str) and cat.strip():
        parts.append(cat.strip())
    return " ".join(parts)
