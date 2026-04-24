"""
Himalayas — public remote jobs JSON API (no auth).

API: GET https://himalayas.app/jobs/api/search?q=...&page=...
Docs: https://himalayas.app/docs/remote-jobs-api
OpenAPI: https://himalayas.app/docs/openapi.json

Terms: credit Himalayas when presenting results (see API terms).
"""

from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Any, Optional

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_SEARCH_URL = "https://himalayas.app/jobs/api/search"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://himalayas.app/",
}
TIMEOUT = 45
PAGE_SIZE = 20
MAX_PAGES_PER_QUERY = 10
REQUEST_DELAY_SEC = 0.45
RATE_LIMIT_RETRY_WAIT_SEC = 60

# Complementary queries; merged and deduped by application URL.
SEARCH_QUERIES: tuple[str, ...] = ("frontend", "typescript", "angular")

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


class HimalayasSource(BaseSource):
    name = "himalayas"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for q in SEARCH_QUERIES:
            page = 1
            total_count: Optional[int] = None
            while page <= MAX_PAGES_PER_QUERY:
                if page > 1:
                    time.sleep(REQUEST_DELAY_SEC)
                payload = self._fetch_search(q, page)
                if not payload:
                    break
                if total_count is None:
                    tc = payload.get("totalCount")
                    if isinstance(tc, int) and tc >= 0:
                        total_count = tc
                batch = payload.get("jobs")
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
                    f"[Himalayas] q={q!r} page={page} -> +{len(batch)} raw "
                    f"(unique total {len(jobs)})"
                )
                if total_count is not None:
                    if page * PAGE_SIZE >= total_count:
                        break
                if len(batch) < PAGE_SIZE:
                    break
                page += 1

        logger.info(f"[Himalayas] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_search(self, q: str, page: int) -> Optional[dict[str, Any]]:
        params = {"q": q, "page": page}
        for _ in range(2):
            try:
                resp = requests.get(
                    API_SEARCH_URL,
                    params=params,
                    headers=HEADERS,
                    timeout=TIMEOUT,
                )
                if resp.status_code == 429:
                    logger.warning(
                        f"[Himalayas] rate limited (429), waiting {RATE_LIMIT_RETRY_WAIT_SEC}s"
                    )
                    time.sleep(RATE_LIMIT_RETRY_WAIT_SEC)
                    continue
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, dict):
                    return None
                return data
            except Exception as e:
                logger.warning(f"[Himalayas] search q={q!r} page={page} failed: {e}")
                return None
        return None

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("companyName") or "").strip()
        url = (raw.get("applicationLink") or "").strip()
        if not title or not company or not url:
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


def _format_location(raw: dict) -> str:
    locs = raw.get("locationRestrictions")
    if not isinstance(locs, list) or len(locs) == 0:
        return "Worldwide"
    names: list[str] = []
    for item in locs:
        if isinstance(item, dict):
            n = (item.get("name") or "").strip()
            if n:
                names.append(n)
    if not names:
        return "Worldwide"
    return ", ".join(names)


def _format_salary(raw: dict) -> Optional[str]:
    cur = (raw.get("currency") or "").strip() or "USD"
    lo = raw.get("minSalary")
    hi = raw.get("maxSalary")
    try:
        lo_i = int(float(lo)) if lo is not None else 0
    except (TypeError, ValueError):
        lo_i = 0
    try:
        hi_i = int(float(hi)) if hi is not None else 0
    except (TypeError, ValueError):
        hi_i = 0
    if lo_i <= 0 and hi_i <= 0:
        return None
    def fmt(n: int) -> str:
        return f"{n:,}".replace(",", " ")
    if lo_i and hi_i:
        return f"{fmt(lo_i)}–{fmt(hi_i)} {cur}/yr"
    if lo_i:
        return f"{fmt(lo_i)}+ {cur}/yr"
    return f"up to {fmt(hi_i)} {cur}/yr"


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    ex = raw.get("excerpt")
    if isinstance(ex, str) and ex.strip():
        parts.append(ex.strip())
    for key in ("categories", "parentCategories"):
        val = raw.get(key)
        if isinstance(val, list):
            parts.append(" ".join(str(x) for x in val))
    desc = raw.get("description")
    if isinstance(desc, str) and desc:
        text = unescape(_HTML_TAG_RE.sub(" ", desc))
        text = re.sub(r"\s+", " ", text).strip()
        parts.append(text[:800])
    return " ".join(parts)
