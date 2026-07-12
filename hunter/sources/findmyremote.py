"""
FindMyRemote.ai — public remote-jobs JSON API (no auth, no Cloudflare).

Listing: GET https://findmyremote.ai/api/jobs?query=...
  -> {"totalCount": N, "jobs": [...], "cached": bool}
  The server returns only the ~21 freshest matches, newest-first; page/offset/
  limit params are silently ignored (live-verified 2026-07-12 — same contract
  as NoFluffJobs's ignored `page` field, and equally fine: those are the
  freshest postings, which is all a scheduled hunt needs).
Detail:  GET https://findmyremote.ai/api/jobs/{job-slug}
  -> {"job": {..., "description": "<p>…</p>", "dateDeleted": ...}}
  Deleted jobs stay in the API with `dateDeleted` set, while their HTML page
  (findmyremote.ai/companies/{c}/jobs/{slug}) starts returning 404.

Each listing hit carries the ORIGINAL external ATS url (SmartRecruiters /
Lever / Workable / Greenhouse / Ashby / Teamtailor / …) — that url becomes
``job.url`` so dedup works across sources (the same posting found via the ATS
aggregator or Gmail alerts collapses) and the tracker link points at the real
application page. Detail-page fetch for those urls dispatches through the
normal roster (ats_aggregator claims Workable/Greenhouse/Lever/Ashby;
SmartRecruiters/Teamtailor extract fine via html_fallback — live-verified).

``matches_url``/``fetch_text`` still claim findmyremote.ai urls because the
Telegram channel `findmyremote_frontend` (run by the same site, already our
top-yield channel in hunter/sources/telegram_channels.py) relays
findmyremote.ai/companies/{c}/jobs/{slug} permalinks. Those pages are Next.js
RSC shells that 404 once the job is deleted — the generic HTML fallback FAILed
on 100% of them (tracker rows 2026-07-11); the API is the only reliable text
path. A `dateDeleted` job returns a synthetic expired marker that
hunter.expired_check.is_job_expired recognizes, so a stale channel link
becomes a clean $0 EXPIRED skip instead of a FAIL row.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import ensure_remote_token, strip_html

logger = logging.getLogger(__name__)

API_JOBS_URL = "https://findmyremote.ai/api/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://findmyremote.ai/jobs",
}
TIMEOUT = 45
REQUEST_DELAY_SEC = 0.45

# Complementary queries; merged and deduped by url. Each returns only the ~21
# freshest matches, so the union stays small and the central filter prunes it.
SEARCH_QUERIES: tuple[str, ...] = ("angular", "frontend", "react")

# Matches EXPIRED_PATTERNS in hunter/expired_check.py — the apply pipeline's
# Step 3 turns this into a clean EXPIRED skip before any LLM spend.
_EXPIRED_TEXT = "This job posting has expired."

# The listing's `countries` field is ISO codes; keep them as-is for the card
# except the ones the listing-level filters gate on by name.
_COUNTRY_NAMES = {"ru": "Russia", "by": "Belarus", "pl": "Poland"}
_MAX_COUNTRIES_SHOWN = 8


class FindMyRemoteSource(BaseSource):
    name = "findmyremote"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "findmyremote.ai" in host

    def fetch_text(self, url: str) -> str:
        """Fetch full posting text for a findmyremote.ai page url via the API.

        Only Telegram-channel-relayed permalinks land here — search() emits the
        external ATS url, which dispatches to that ATS's own fetcher.
        """
        slug = _job_slug_from_url(url)
        if slug:
            try:
                data = self._fetch_job(slug)
            except Exception as e:
                logger.warning(f"[FindMyRemote] detail API failed for {slug!r} ({e})")
                data = None
            if data:
                if data.get("dateDeleted"):
                    return _EXPIRED_TEXT
                text = _format_job_text(data)
                if text:
                    return text
        from hunter.sources.html_fallback import fetch_html

        return fetch_html(url)

    def _fetch_job(self, slug: str) -> Optional[dict[str, Any]]:
        resp = requests.get(f"{API_JOBS_URL}/{slug}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        job = data.get("job") if isinstance(data, dict) else None
        return job if isinstance(job, dict) else None

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
            logger.info(f"[FindMyRemote] query={q!r} -> {len(batch)} raw, +{taken} new")

        logger.info(f"[FindMyRemote] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_search(self, query: str) -> list[dict]:
        try:
            resp = requests.get(
                API_JOBS_URL,
                params={"query": query},
                headers=HEADERS,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[FindMyRemote] search query={query!r} failed: {e}")
            return []
        batch = data.get("jobs") if isinstance(data, dict) else None
        return batch if isinstance(batch, list) else []

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company_raw = raw.get("company") if isinstance(raw.get("company"), dict) else {}
        company = (company_raw.get("name") or "").strip()
        url = (raw.get("url") or "").strip()
        if not url:
            # No external ATS link — fall back to the site's own job page.
            slug = (raw.get("slug") or "").strip()
            company_slug = (company_raw.get("slug") or "").strip()
            if slug and company_slug:
                url = f"https://findmyremote.ai/companies/{company_slug}/jobs/{slug}"
        if not title or not company or not url:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw),
            salary=None,  # listing API carries no salary field
            url=url,
            source=self.name,
            raw=raw,
        )


def _job_slug_from_url(url: str) -> str:
    """Extract the job slug from a findmyremote.ai url.

    Shapes: /companies/{company-slug}/jobs/{job-slug} and /jobs/{job-slug}.
    The job slug doubles as the detail-API path segment.
    """
    parts = [p for p in urlparse(url).path.split("/") if p]
    for i, part in enumerate(parts):
        if part == "jobs" and i + 1 < len(parts):
            return parts[i + 1]
    return ""


def _format_location(raw: dict) -> str:
    countries = raw.get("countries")
    names: list[str] = []
    if isinstance(countries, list):
        for code in countries[:_MAX_COUNTRIES_SHOWN]:
            if isinstance(code, str) and code.strip():
                c = code.strip().lower()
                names.append(_COUNTRY_NAMES.get(c, c.upper()))
        if len(countries) > _MAX_COUNTRIES_SHOWN:
            names.append(f"+{len(countries) - _MAX_COUNTRIES_SHOWN} more")
    return ensure_remote_token("Remote", geo=", ".join(names))


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
    ext_url = (job.get("url") or "").strip()
    if ext_url:
        lines.append(f"Apply: {ext_url}")
    desc = strip_html(job.get("description"), 20000)
    if not desc:
        return ""
    lines.append("")
    lines.append(desc)
    return "\n".join(lines)


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    skills = raw.get("skills")
    if isinstance(skills, list):
        parts.append(" ".join(str(s) for s in skills))
    return " ".join(parts)
