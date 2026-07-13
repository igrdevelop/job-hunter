"""
Smart Jobs (thesmartjobs.com) — Polish IT job board on the Traffit ATS.

Public JSON API, no auth, no Cloudflare (live-verified 2026-07-13 — a plain
server-side GET returns JSON, same as the browser):

Listing: GET https://thesmartjobs.com/api/jobs/search
             ?query=<kw>&sort=freshness&limit=100&locale=en
  -> {"data": [ {job}, ... ], "meta": {"limit", "page", "total", "totalPages"}}
  `sort=freshness` is strictly newest-first; page 1 (the 100 freshest per query)
  is all a scheduled hunt needs. Each listing hit already carries the FULL HTML
  `description`, but detail-page fetch still goes through the detail endpoint so
  a deleted posting reads as EXPIRED rather than FAIL (see below).

Detail:  GET https://thesmartjobs.com/api/jobs/{slug}?locale=en
  -> the bare job object (title, company, description, status, locations, …).
  `slug` is the LAST path segment of the public url (after `praca/`). A deleted
  posting returns HTTP 404 `{"error":"Job not found"}` — `fetch_text` turns that
  into a synthetic expired marker that hunter.expired_check.is_job_expired
  recognizes, so a stale link becomes a clean $0 EXPIRED skip.

Public url: https://thesmartjobs.com/en/{slugUrl}  (slugUrl == "praca/<slug>").

Poland-focused (Warsaw/Wrocław/Kraków/… + remote), so the candidate's home
market. Location is built from workModes + the per-city `locations`; the central
whitelist (remote/zdalnie/wrocław) drops other-city on-site/hybrid roles, and a
remote workMode injects a "remote" token so a genuinely remote offer survives.
"""

from __future__ import annotations

import logging
import time
from typing import Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import ensure_remote_token, strip_html

logger = logging.getLogger(__name__)

API_SEARCH_URL = "https://thesmartjobs.com/api/jobs/search"
API_DETAIL_URL = "https://thesmartjobs.com/api/jobs/{slug}"
PUBLIC_URL_TMPL = "https://thesmartjobs.com/en/{slug_url}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://thesmartjobs.com/en",
}
TIMEOUT = 45
REQUEST_DELAY_SEC = 0.6
SEARCH_LIMIT = 100

# Complementary queries; merged and deduped by url. Each returns the freshest
# matches first, so the union stays fresh and the central filter prunes it.
SEARCH_QUERIES: tuple[str, ...] = ("angular", "frontend", "react")

# Matches EXPIRED_PATTERNS in hunter/expired_check.py — the apply pipeline's
# Step 3 turns this into a clean EXPIRED skip before any LLM spend.
_EXPIRED_TEXT = "This job posting has expired."

# contractType codes -> short human labels for the salary line.
_CONTRACT_LABELS = {
    "b2b": "B2B",
    "employmentContract": "UoP",
    "mandateContract": "UZ",
    "contractWork": "UoD",
    "internship": "Internship",
}
_MAX_DESC_LEN = 20000


class TheSmartJobsSource(BaseSource):
    name = "thesmartjobs"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "thesmartjobs.com" in host

    def fetch_text(self, url: str) -> str:
        """Fetch full posting text for a thesmartjobs.com url via the detail API.

        A deleted posting (HTTP 404) returns the synthetic expired marker so the
        pipeline records a clean EXPIRED skip instead of a FAIL row.
        """
        slug = _slug_from_url(url)
        if slug:
            try:
                resp = requests.get(
                    API_DETAIL_URL.format(slug=slug),
                    params={"locale": "en"},
                    headers=HEADERS,
                    timeout=TIMEOUT,
                )
            except Exception as e:
                logger.warning(f"[SmartJobs] detail fetch failed for {slug!r} ({e})")
            else:
                if resp.status_code == 404:
                    return _EXPIRED_TEXT
                try:
                    resp.raise_for_status()
                    data = resp.json()
                except Exception as e:
                    logger.warning(f"[SmartJobs] detail parse failed for {slug!r} ({e})")
                    data = None
                if isinstance(data, dict):
                    if str(data.get("status") or "").lower() in ("closed", "archived", "expired"):
                        return _EXPIRED_TEXT
                    text = _format_job_text(data)
                    if text:
                        return text
        from hunter.sources.html_fallback import fetch_html

        return fetch_html(url)

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for i, q in enumerate(SEARCH_QUERIES):
            if i > 0:
                time.sleep(REQUEST_DELAY_SEC)
            batch = self._fetch_search(q)
            taken = 0
            for raw in batch:
                if not isinstance(raw, dict):
                    continue
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                if not self.matches_coarse_prefilter(job.title, _prefilter_context(raw)):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)
                taken += 1
            logger.info(f"[SmartJobs] query={q!r} -> {len(batch)} raw, +{taken} new")

        logger.info(f"[SmartJobs] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_search(self, query: str) -> list[dict]:
        try:
            resp = requests.get(
                API_SEARCH_URL,
                params={
                    "query": query,
                    "sort": "freshness",
                    "limit": SEARCH_LIMIT,
                    "locale": "en",
                },
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[SmartJobs] search query={query!r} failed: {e}")
            return []
        batch = data.get("data") if isinstance(data, dict) else None
        return batch if isinstance(batch, list) else []

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        if not title:
            role = raw.get("role") if isinstance(raw.get("role"), dict) else {}
            title = (role.get("name") or "").strip()
        company_raw = raw.get("company") if isinstance(raw.get("company"), dict) else {}
        company = (company_raw.get("name") or "").strip()
        slug_url = (raw.get("slugUrl") or "").strip().strip("/")
        if not slug_url:
            slug = (raw.get("slug") or "").strip()
            if slug:
                slug_url = f"praca/{slug}"
        if not title or not company or not slug_url:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw),
            salary=_format_salary(raw),
            url=PUBLIC_URL_TMPL.format(slug_url=slug_url),
            source=self.name,
            raw=raw,
        )


def _slug_from_url(url: str) -> str:
    """Return the detail-API slug (last path segment) from a public job url.

    https://thesmartjobs.com/en/praca/programista-frontend-mid-9044bedb
      -> programista-frontend-mid-9044bedb
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    return parts[-1] if parts else ""


def _format_location(raw: dict) -> str:
    work_modes = raw.get("workModes")
    is_remote = isinstance(work_modes, list) and any(str(m).lower() == "remote" for m in work_modes)
    cities: list[str] = []
    locations = raw.get("locations")
    if isinstance(locations, list):
        for loc in locations:
            if not isinstance(loc, dict):
                continue
            city = (loc.get("city") or loc.get("displayName") or "").strip()
            if city and city not in cities:
                cities.append(city)
    geo = ", ".join(cities)
    if is_remote:
        return ensure_remote_token("Remote", geo=geo or None)
    # On-site / hybrid: return the raw city string; the central location
    # whitelist (remote/zdalnie/wrocław) decides whether it survives.
    return geo


def _format_salary(raw: dict) -> Optional[str]:
    salaries = raw.get("salaries")
    if not isinstance(salaries, list) or not salaries:
        return None
    s = salaries[0]
    if not isinstance(s, dict):
        return None
    lo, hi = s.get("min"), s.get("max")
    currency = (s.get("currency") or "").strip()
    contract = _CONTRACT_LABELS.get(s.get("contractType"), (s.get("contractType") or "").strip())
    amount = ""
    if isinstance(lo, (int, float)) and isinstance(hi, (int, float)):
        amount = f"{_thousands(lo)}–{_thousands(hi)}"
    elif isinstance(lo, (int, float)):
        amount = f"{_thousands(lo)}+"
    elif isinstance(hi, (int, float)):
        amount = f"up to {_thousands(hi)}"
    parts = [p for p in (amount, currency, contract) if p]
    return " ".join(parts) or None


def _thousands(n: float) -> str:
    return f"{int(n):,}".replace(",", " ")


def _format_job_text(job: dict) -> str:
    """Render the detail-API payload into plain posting text for the pipeline."""
    company_raw = job.get("company") if isinstance(job.get("company"), dict) else {}
    lines: list[str] = []
    title = (job.get("title") or "").strip()
    company = (company_raw.get("name") or "").strip()
    if title:
        lines.append(title)
    if company:
        lines.append(f"Company: {company}")
    loc = _format_location(job)
    if loc:
        lines.append(f"Location: {loc}")
    salary = _format_salary(job)
    if salary:
        lines.append(f"Salary: {salary}")
    apply_url = (job.get("applicationFormUrl") or "").strip()
    if apply_url:
        lines.append(f"Apply: {apply_url}")
    desc = strip_html(job.get("description"), _MAX_DESC_LEN)
    if not desc:
        return ""
    lines.append("")
    lines.append(desc)
    return "\n".join(lines)


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    role = raw.get("role") if isinstance(raw.get("role"), dict) else {}
    if role.get("name"):
        parts.append(str(role["name"]))
    attributes = raw.get("attributes")
    if isinstance(attributes, list):
        for attr in attributes:
            if isinstance(attr, dict) and attr.get("attributeName"):
                parts.append(str(attr["attributeName"]))
    desc = strip_html(raw.get("description"), 1500)
    if desc:
        parts.append(desc)
    return " ".join(parts)
