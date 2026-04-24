"""
Remotive — remote job board JSON API.

API: GET https://remotive.com/api/remote-jobs
Docs: https://github.com/remotive-com/remote-jobs-api

Terms: link back to job URL on Remotive, credit source; do not hammer the API
(a few requests per day is enough — we use two filtered GETs per hunt).
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Any, Optional

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://remotive.com/api/remote-jobs"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://remotive.com/",
}
TIMEOUT = 45

# Two complementary queries; results are merged and deduped by URL.
SEARCH_PARAMS: tuple[dict[str, str], ...] = (
    {"category": "software-dev"},
    {"search": "frontend"},
)

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


class RemotiveSource(BaseSource):
    name = "remotive"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for params in SEARCH_PARAMS:
            try:
                batch = self._fetch(params)
            except Exception as e:
                logger.warning(f"[Remotive] fetch {params} failed: {e}")
                continue
            for raw in batch:
                job = self._parse(raw)
                if not job or job.url in seen_urls:
                    continue
                ctx = _text_preview(raw.get("description"), 800)
                tags = raw.get("tags")
                if isinstance(tags, list):
                    ctx = f"{ctx} {' '.join(str(t) for t in tags)}"
                if not self.matches_coarse_prefilter(job.title, ctx):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[Remotive] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch(self, params: dict[str, str]) -> list[dict[str, Any]]:
        resp = requests.get(API_URL, params=params, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        jobs = data.get("jobs")
        if not isinstance(jobs, list):
            return []
        logger.info(f"[Remotive] params={params} -> {len(jobs)} raw (job-count={data.get('job-count')})")
        return jobs

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company_name") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not company or not url:
            return None
        salary = (raw.get("salary") or "").strip() or None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw),
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )


def _format_location(raw: dict) -> str:
    """All Remotive listings are remote; keep region hint for the user's location filter."""
    loc = (raw.get("candidate_required_location") or "").strip()
    if not loc:
        return "Remote"
    low = loc.lower()
    if low in ("worldwide", "anywhere", "anywhere in the world", "global"):
        return "Remote"
    return f"{loc} (Remote)"


def _text_preview(html_fragment: Optional[str], max_len: int) -> str:
    if not html_fragment:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html_fragment))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]
