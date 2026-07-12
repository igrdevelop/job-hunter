"""
Bulldogjob.com source.

Strategy (tested 2026-04):
  1. Fetch listing page for Angular mid/senior jobs via HTTPS.
     The page contains __NEXT_DATA__ JSON with up to 21+ job objects.
  2. Parse jobs directly from pageProps.jobs — no extra detail calls needed
     for filtering; Job objects are built from listing-level data.
  3. Individual job description text is fetched lazily by job_fetch/bulldogjob.py
     only when LLM processing is triggered.

Listing URL: https://bulldogjob.com/companies/jobs/s/skills,Angular/experience,mid,senior
Job page URL: https://bulldogjob.com/companies/jobs/{job_id}
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://bulldogjob.com"

# /order,published,desc: without it the listing uses Bulldogjob's default
# (relevance/promoted) order, which pins sponsored offers above fresh ones and
# can push a new posting out of the first page entirely. Live-verified
# 2026-07-10: the segment is echoed into __NEXT_DATA__ as
# order={"field":"PUBLISHED","direction":"DESC"} and reorders the main job
# block strictly newest-first (a trailing "recommended" block stays put).
LISTING_URLS = [
    f"{BASE}/companies/jobs/s/skills,Angular/experience,mid,senior/order,published,desc",
    f"{BASE}/companies/jobs/s/skills,Angular/experience,mid,senior/remote,true/order,published,desc",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

TIMEOUT = 20


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section|strong|em)[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_job_id(url: str) -> str:
    """Extract job ID slug from URL like /companies/jobs/{id}."""
    m = re.search(r"/companies/jobs/([^/?#]+)", url)
    if not m:
        raise ValueError(f"Cannot extract Bulldogjob job ID from URL: {url}")
    return m.group(1)


def _build_detail_text(job: dict, apollo: dict) -> str:
    parts: list[str] = []

    title = (job.get("position") or "N/A").strip()
    parts.append(f"Job Title: {title}")

    company_ref = job.get("company", {})
    if isinstance(company_ref, dict):
        ref_key = company_ref.get("__ref", "")
        company_data = apollo.get(ref_key, {})
        company = (company_data.get("name") or "N/A").strip()
    else:
        company = "N/A"
    parts.append(f"Company: {company}")

    locations = job.get("locations", [])
    loc_parts: list[str] = []
    for loc in locations:
        if isinstance(loc, dict):
            loc_inner = loc.get("location", {})
            city = (loc_inner.get("cityEn") or loc_inner.get("cityPl") or "").strip()
            if city:
                loc_parts.append(city)
    is_remote = job.get("remote", False)
    work_modes = job.get("workModes") or []
    if is_remote or "full-remote" in work_modes:
        loc_parts.append("Remote")
    if loc_parts:
        parts.append(f"Location: {', '.join(loc_parts)}")

    level = (job.get("experienceLevel") or "").strip()
    if level:
        parts.append(f"Experience level: {level}")

    main_tech = (job.get("mainTechnology") or "").strip()
    tags = job.get("technologyTags") or []
    if main_tech:
        parts.append(f"Main technology: {main_tech}")
    if tags:
        parts.append(f"Technologies: {', '.join(tags)}")

    for sal_key in ("b2bSalary", "employmentSalary"):
        sal = job.get(sal_key) or {}
        money = sal.get("money")
        currency = (sal.get("currency") or "PLN").upper()
        timeframe = sal.get("timeframe", "")
        if money:
            label = f"{sal_key.replace('Salary', '')} salary: {money} {currency}"
            if timeframe:
                label += f"/{timeframe}"
            parts.append(label)
            break

    offer_html = (job.get("offer") or "").strip()
    if offer_html:
        parts.append(f"\n--- Offer / Description ---\n{_strip_html(offer_html)}")

    req_html = (job.get("requirements") or "").strip()
    if req_html:
        parts.append(f"\n--- Requirements ---\n{_strip_html(req_html)}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError("Bulldogjob page returned almost no content for job")
    return text


class BulldogJobSource(BaseSource):
    name = "bulldogjob"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "bulldogjob.com" in host

    def fetch_text(self, url: str) -> str:
        """Fetch a Bulldogjob posting and pull data out of __NEXT_DATA__/Apollo state."""
        job_id = _extract_job_id(url)
        job_url = f"{BASE}/companies/jobs/{job_id}"

        resp = requests.get(job_url, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()

        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            resp.text,
            re.S,
        )
        if not m:
            raise ValueError(f"No __NEXT_DATA__ on Bulldogjob page: {job_url}")

        data = json.loads(m.group(1))
        apollo = data.get("props", {}).get("pageProps", {}).get("__APOLLO_STATE__", {})

        job_key = f"Job:{job_id}"
        job_data = apollo.get(job_key)
        if not job_data:
            for key, val in apollo.items():
                if key.startswith("Job:") and isinstance(val, dict):
                    job_data = val
                    break

        if not job_data:
            raise ValueError(f"No Job data in APOLLO_STATE for {job_id}")

        return _build_detail_text(job_data, apollo)

    def search(self) -> list[Job]:
        seen_ids: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            raw_jobs = self._fetch_listing(url)
            logger.info(f"[Bulldogjob] {url} → {len(raw_jobs)} raw jobs")
            for raw in raw_jobs:
                job_id = raw.get("id", "")
                if not job_id or job_id in seen_ids:
                    continue
                if not self._is_relevant(raw):
                    continue
                seen_ids.add(job_id)
                job = self._parse(raw)
                if job:
                    jobs.append(job)

        logger.info(f"[Bulldogjob] {len(jobs)} jobs after pre-filter")
        return jobs

    # ── Listing fetch ─────────────────────────────────────────────────────────

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            m = re.search(
                r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
                resp.text,
                re.S,
            )
            if not m:
                logger.warning(f"[Bulldogjob] No __NEXT_DATA__ on {url}")
                return []
            data = json.loads(m.group(1))
            jobs = data.get("props", {}).get("pageProps", {}).get("jobs", [])
            return jobs if isinstance(jobs, list) else []
        except Exception as e:
            logger.error(f"[Bulldogjob] fetch listing {url}: {e}")
            return []

    # ── Pre-filter (before building Job object) ───────────────────────────────

    def _is_relevant(self, raw: dict) -> bool:
        """Quick keyword pre-filter on title + tags."""
        title = (raw.get("position") or "").lower()
        tags = [t.lower() for t in (raw.get("technologyTags") or [])]
        level = (raw.get("experienceLevel") or "").lower()
        main_tech = (raw.get("mainTechnology") or "").lower()

        # Exclude juniors / interns
        exclude_levels = [lv.lower() for lv in FILTER.get("exclude_levels", [])]
        if level in exclude_levels:
            return False

        combined = title + " " + " ".join(tags) + " " + main_tech

        if FILTER.get("require_angular", False):
            return "angular" in combined

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        return any(kw in combined for kw in keywords)

    # ── Parser ────────────────────────────────────────────────────────────────

    def _parse(self, raw: dict) -> Optional[Job]:
        job_id = (raw.get("id") or "").strip()
        title = (raw.get("position") or "").strip()
        company_data = raw.get("company") or {}
        company = (company_data.get("name") or "").strip()

        if not job_id or not title or not company:
            return None

        url = f"{BASE}/companies/jobs/{job_id}"
        location = self._parse_location(raw)
        salary = self._parse_salary(raw)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _parse_location(raw: dict) -> str:
        is_remote = raw.get("remote", False)
        city = (raw.get("city") or "").strip()

        if is_remote and not city:
            return "Remote"
        if is_remote and city:
            return f"{city} (Remote)"
        if city:
            return city
        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        sal = raw.get("denominatedSalaryLong") or {}
        money = sal.get("money")
        currency = (sal.get("currency") or "PLN").upper()

        if money:
            return f"{money} {currency}"
        return None
