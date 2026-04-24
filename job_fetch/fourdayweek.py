"""Fetch 4dayweek.io job text via public API v2 (GET /api/v2/jobs/{slug})."""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)

API_JOB_URL = "https://4dayweek.io/api/v2/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://4dayweek.io/",
}
TIMEOUT = 45

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
    slug = segments[-1]
    slug = slug.split("?", 1)[0]
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


def fetch_fourdayweek(url: str) -> str:
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
