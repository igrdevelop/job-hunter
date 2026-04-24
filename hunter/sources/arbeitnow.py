"""
Arbeitnow — free EU-focused job board API (ATS-fed listings).

API: GET https://www.arbeitnow.com/api/job-board-api?page={n}
Docs: https://www.arbeitnow.com/blog/job-board-api

Paginate until a page returns no jobs or MAX_PAGES is reached.
"""

from __future__ import annotations

import logging
import re
import time
from html import unescape
from typing import Optional

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://www.arbeitnow.com/api/job-board-api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://www.arbeitnow.com/",
}
TIMEOUT = 30
MAX_PAGES = 12
PAGE_DELAY_SEC = 0.45

_HTML_TAG_RE = re.compile(r"<[^>]+>")


class ArbeitnowSource(BaseSource):
    name = "arbeitnow"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for page in range(1, MAX_PAGES + 1):
            if page > 1:
                time.sleep(PAGE_DELAY_SEC)
            try:
                batch = self._fetch_page(page)
            except Exception as e:
                logger.warning(f"[Arbeitnow] page {page} failed: {e}")
                break
            if not batch:
                break
            for raw in batch:
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                ctx = _text_preview(raw.get("description"), 600)
                if not self.matches_coarse_prefilter(job.title, ctx):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)
            logger.info(f"[Arbeitnow] page {page} -> +{len(batch)} raw (total unique {len(jobs)})")

        logger.info(f"[Arbeitnow] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_page(self, page: int) -> list[dict]:
        resp = requests.get(
            API_URL,
            params={"page": page},
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        payload = resp.json()
        data = payload.get("data")
        if not isinstance(data, list):
            return []
        return data

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company_name") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not company or not url:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw),
            salary=None,
            url=url,
            source=self.name,
            raw=raw,
        )


def _format_location(raw: dict) -> str:
    loc = (raw.get("location") or "").strip()
    if raw.get("remote"):
        if loc:
            return f"{loc} (Remote)"
        return "Remote"
    return loc or "Unknown"


def _text_preview(html_fragment: Optional[str], max_len: int) -> str:
    if not html_fragment:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html_fragment))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]
