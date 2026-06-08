"""
JustRemote (justremote.co) — remote-first job board, JSON API.

Strategy: the SPA talks to a public JSON API on a separate host
(``justremote-api.herokuapp.com``). The listing endpoint
``/api/v1/jobs?category=developer`` returns the ~10 newest developer roles
(skill sub-filtering like angular/react happens client-side and the API ignores
it, so this is a low-volume trickle). The single-job endpoint
``/api/v1/jobs/{slug}`` returns the full posting, which fetch_text assembles into
plain text — no SPA scraping needed.

Listing API: https://justremote-api.herokuapp.com/api/v1/jobs?category=developer
Canonical job URL: https://justremote.co/{href}   (href e.g. "remote-developer-jobs/<slug>")
Detail API: https://justremote-api.herokuapp.com/api/v1/jobs/{slug}
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import ensure_remote_token, strip_html

logger = logging.getLogger(__name__)

SITE = "https://justremote.co"
API = "https://justremote-api.herokuapp.com/api/v1/jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": SITE,
    "Referer": SITE + "/",
}
TIMEOUT = 30
LISTING_PARAMS = {"category": "developer"}

# Sections of the single-job payload concatenated into the LLM job text.
_DETAIL_SECTIONS = ("about_role", "who_looking_for", "our_offer", "about_company")


class JustRemoteSource(BaseSource):
    name = "justremote"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "justremote.co" in host

    def search(self) -> list[Job]:
        try:
            raw_jobs = self._fetch_listing()
        except Exception as e:
            logger.warning(f"[justremote] listing failed: {e}")
            return []

        logger.info(f"[justremote] listing returned {len(raw_jobs)} raw")
        seen_urls: set[str] = set()
        jobs: list[Job] = []
        for raw in raw_jobs:
            job = self._parse(raw)
            if not job or job.url in seen_urls:
                continue
            ctx = f"{raw.get('category', '')} {raw.get('remote_type', '')}"
            if not self.matches_coarse_prefilter(job.title, ctx):
                continue
            seen_urls.add(job.url)
            jobs.append(job)

        logger.info(f"[justremote] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_listing(self) -> list[dict[str, Any]]:
        resp = requests.get(API, headers=HEADERS, params=LISTING_PARAMS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company_name") or "").strip()
        href = (raw.get("href") or "").strip().lstrip("/")
        if not title or not company or not href:
            return None
        if raw.get("is_active") is False:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw.get("remote_type"), raw.get("location_restrictions")),
            salary=None,
            url=f"{SITE}/{href}",
            source=self.name,
            raw=raw,
        )

    def fetch_text(self, url: str) -> str:
        slug = _slug_from_url(url)
        if slug:
            try:
                text = self._fetch_detail(slug)
                if text:
                    return text
            except Exception as e:
                logger.warning(f"[justremote] detail fetch failed ({e}), using html_fallback")
        from hunter.sources.html_fallback import fetch_html

        return fetch_html(url)

    def _fetch_detail(self, slug: str) -> str:
        resp = requests.get(f"{API}/{slug}", headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            return ""
        parts: list[str] = []
        title = (data.get("title") or "").strip()
        if title:
            parts.append(title)
        for key in _DETAIL_SECTIONS:
            section = strip_html(data.get(key), 8000)
            if section:
                parts.append(section)
        return "\n\n".join(parts).strip()


def _slug_from_url(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]


def _format_location(remote_type: Any, restrictions: Any) -> str:
    """Build a location string that always carries a 'remote' token so the central
    location whitelist keeps it, while preserving any geographic restriction hint."""
    base = str(remote_type).strip() if remote_type else ""
    geo = ""
    if isinstance(restrictions, list):
        geo = ", ".join(str(r).strip() for r in restrictions if r and str(r).strip())
    return ensure_remote_token(base, geo)
