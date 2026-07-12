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
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import strip_html

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


class HimalayasSource(BaseSource):
    name = "himalayas"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "himalayas.app" in host

    def fetch_text(self, url: str) -> str:
        """Re-query the search API by company slug and return the stored description.

        Every Himalayas job's ``applicationLink`` points back at a himalayas.app
        page (there is no external-ATS variant observed) and that page 403s to a
        plain ``requests.get`` — live-verified as a genuine Cloudflare Turnstile
        challenge (``Cf-Mitigated: challenge``), not just a header/UA check — so
        the generic HTML fallback this source used to rely on failed on 100% of
        Himalayas jobs. The public search API has no per-job GET endpoint, but it
        already carries the full ``description`` in every hit, so we recover the
        text without ever hitting the blocked HTML page: first a cheap
        ``?company=<slug>`` lookup (works for the common case — most companies
        have only a handful of postings, so the target is on page 1), and if
        that misses (a staffing agency with hundreds of listings can bury it
        past page 1) a second ``?company=<slug>&q=<title words>`` retry — the
        free-text query re-ranks by relevance to the job's own title, which
        reliably surfaces it (live-verified against a 359-listing company).
        """
        slug = _company_slug_from_url(url)
        if slug:
            try:
                desc = self._fetch_description(slug, url)
                if desc:
                    return desc
                q = _title_query_from_url(url)
                if q:
                    desc = self._fetch_description(slug, url, q=q)
                    if desc:
                        return desc
            except Exception as e:
                logger.warning(f"[Himalayas] company lookup failed ({e}), using html_fallback")
        from hunter.sources.html_fallback import fetch_html

        return fetch_html(url)

    def _fetch_description(self, company_slug: str, url: str, q: Optional[str] = None) -> str:
        params: dict[str, str] = {"company": company_slug}
        if q:
            params["q"] = q
        resp = requests.get(API_SEARCH_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        for raw in data.get("jobs", []):
            if not isinstance(raw, dict):
                continue
            if (raw.get("applicationLink") or "").strip() == url:
                return strip_html(raw.get("description"), 20000)
        return ""

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


def _company_slug_from_url(url: str) -> str:
    """Extract the company slug from a himalayas.app job URL.

    URL shape: https://himalayas.app/companies/{company-slug}/jobs/{job-slug}
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "companies":
        return parts[1]
    return ""


def _title_query_from_url(url: str) -> str:
    """Best-effort free-text query from the job-slug part of a himalayas.app URL.

    Strips Himalayas' trailing numeric dedup suffix (e.g. ``-4409560950``) and
    turns hyphens into spaces, so the result reads like the job title and can
    be passed as the search API's ``q`` param to re-rank a large company's
    listings by relevance to this specific posting.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 3 or parts[0] != "companies" or parts[2] != "jobs" or len(parts) < 4:
        return ""
    slug = parts[3]
    slug = re.sub(r"-\d{6,}$", "", slug)
    return slug.replace("-", " ").strip()


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
        parts.append(strip_html(desc, 800))
    return " ".join(parts)
