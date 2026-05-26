"""
4dayweek.io — public JSON API v2 (no auth).

API: GET https://4dayweek.io/api/v2/jobs
Docs: https://4dayweek.io/developers
OpenAPI: https://4dayweek.io/openapi.yaml

Salary fields in API responses are in smallest currency units (e.g. cents); divide by 100 for display.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_LIST_URL = "https://4dayweek.io/api/v2/jobs"
API_JOB_URL = "https://4dayweek.io/api/v2/jobs"  # singular detail uses same path + slug
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


_SLUG_OK = re.compile(r"^[a-z0-9][a-z0-9\-_]{0,200}$", re.I)


def _slug_from_job_url(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if host not in ("4dayweek.io", "www.4dayweek.io"):
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    segments = [s for s in path.split("/") if s]
    if not segments:
        return None
    slug = segments[-1].split("?", 1)[0]
    if not slug or not _SLUG_OK.match(slug):
        return None
    return slug


def _minor_to_major(amount: object) -> Optional[int]:
    if amount is None:
        return None
    try:
        return int(amount) // 100
    except (TypeError, ValueError):
        return None


def _format_salary_block(raw: dict) -> str:
    lo_m = _minor_to_major(raw.get("salary_min"))
    hi_m = _minor_to_major(raw.get("salary_max"))
    cur = (raw.get("salary_currency") or "USD").strip()
    period = (raw.get("salary_period") or "year").strip().lower()
    if (lo_m is None or lo_m <= 0) and (hi_m is None or hi_m <= 0):
        return ""
    period_suffix = {"year": "/yr", "month": "/mo", "hour": "/hr"}.get(period, f"/{period}")

    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", " ")

    lo = lo_m or 0
    hi = hi_m or 0
    if lo and hi:
        text = f"{fmt(lo)}–{fmt(hi)} {cur}{period_suffix}"
    elif lo:
        text = f"{fmt(lo)}+ {cur}{period_suffix}"
    else:
        text = f"up to {fmt(hi)} {cur}{period_suffix}"
    return f"Salary: {text}"


def _location_lines(raw: dict) -> str:
    parts: list[str] = []
    offices = raw.get("office_locations")
    if isinstance(offices, list):
        for o in offices[:8]:
            if not isinstance(o, dict):
                continue
            city = (o.get("city") or "").strip()
            country = (o.get("country") or "").strip()
            if city and country:
                parts.append(f"{city}, {country}")
            elif country:
                parts.append(country)
            elif city:
                parts.append(city)
    allowed = raw.get("remote_allowed")
    if isinstance(allowed, list):
        for a in allowed[:8]:
            if isinstance(a, dict):
                c = (a.get("country") or "").strip()
                if c:
                    parts.append(f"Remote OK: {c}")
    return "; ".join(parts)


def _format_job_plaintext(data: dict) -> str:
    lines: list[str] = []
    title = (data.get("title") or "").strip()
    if title:
        lines.append(title)
    company = data.get("company")
    if isinstance(company, dict):
        cn = (company.get("name") or "").strip()
        if cn:
            lines.append(f"Company: {cn}")
    url = (data.get("url") or "").strip()
    if url:
        lines.append(f"URL: {url}")
    wa = (data.get("work_arrangement") or "").strip()
    if wa:
        lines.append(f"Work arrangement: {wa}")
    if data.get("is_remote"):
        lines.append("Remote: yes")
    loc = _location_lines(data)
    if loc:
        lines.append(f"Location: {loc}")
    sal = _format_salary_block(data)
    if sal:
        lines.append(sal)
    for label, key in (
        ("Category", "category"),
        ("Role", "role"),
        ("Level", "level"),
        ("Schedule", "schedule_type"),
    ):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            lines.append(f"{label}: {v.strip()}")
    tags: list[str] = []
    for key in ("skills", "stack", "tools"):
        val = data.get(key)
        if isinstance(val, list):
            for item in val:
                if isinstance(item, dict):
                    n = (item.get("name") or "").strip()
                    if n:
                        tags.append(n)
    if tags:
        lines.append("Tags: " + ", ".join(tags))
    desc = data.get("description")
    if isinstance(desc, str) and desc.strip():
        lines.append("")
        lines.append(desc.strip())
    return "\n".join(lines)


class FourdayweekSource(BaseSource):
    name = "fourdayweek"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "4dayweek.io" in host

    def fetch_text(self, url: str) -> str:
        from hunter.sources.html_fallback import fetch_html

        slug = _slug_from_job_url(url)
        if not slug:
            logger.info(f"[fourdayweek] no slug from URL, HTML fallback: {url}")
            return fetch_html(url)
        api_url = f"{API_JOB_URL}/{slug}"
        try:
            resp = requests.get(api_url, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 404:
                logger.warning(f"[fourdayweek] API 404 for slug={slug}, HTML fallback")
                return fetch_html(url)
            resp.raise_for_status()
            data = resp.json()
            if not isinstance(data, dict):
                return fetch_html(url)
            return _format_job_plaintext(data)
        except Exception as e:
            logger.warning(f"[fourdayweek] API failed ({e}), HTML fallback")
            return fetch_html(url)

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
