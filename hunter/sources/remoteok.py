"""
Remote OK — public JSON job feed.

API: GET https://remoteok.com/api
First array element is metadata {last_updated, legal}; remaining entries are jobs.

Terms: link back to Remote OK and credit the source (see `legal` in API).
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

API_URL = "https://remoteok.com/api"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Referer": "https://remoteok.com/",
}
TIMEOUT = 45

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


def _extract_job_rows(data: list[Any]) -> list[dict[str, Any]]:
    """Drop API metadata row(s); keep only dicts with a non-empty slug."""
    out: list[dict[str, Any]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        slug = (item.get("slug") or "").strip()
        if not slug:
            continue
        out.append(item)
    return out


class RemoteOkSource(BaseSource):
    name = "remoteok"

    def search(self) -> list[Job]:
        try:
            rows = self._fetch()
        except Exception as e:
            logger.warning(f"[Remote OK] API failed: {e}")
            return []

        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for raw in rows:
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

        logger.info(f"[Remote OK] {len(jobs)} jobs after pre-filter (raw listings {len(rows)})")
        return jobs

    def _fetch(self) -> list[dict[str, Any]]:
        resp = requests.get(API_URL, headers=HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, list):
            return []
        out = _extract_job_rows(data)
        logger.info(f"[Remote OK] fetched {len(out)} job rows")
        return out

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("position") or "").strip()
        company = (raw.get("company") or "").strip()
        slug = (raw.get("slug") or "").strip()
        if not title or not company or not slug:
            return None
        url = f"https://remoteok.com/remote-jobs/{slug}"
        loc = (raw.get("location") or "").strip() or "Remote"
        return Job(
            title=title,
            company=company,
            location=loc,
            salary=_format_salary(raw),
            url=url,
            source=self.name,
            raw=raw,
        )


def _format_salary(raw: dict) -> Optional[str]:
    try:
        lo = int(raw.get("salary_min") or 0)
        hi = int(raw.get("salary_max") or 0)
    except (TypeError, ValueError):
        return None
    if lo <= 0 and hi <= 0:
        return None
    if lo and hi:
        return f"${lo:,}–${hi:,} USD/yr".replace(",", " ")
    if lo:
        return f"${lo:,}+ USD/yr".replace(",", " ")
    return f"up to ${hi:,} USD/yr".replace(",", " ")


def _text_preview(html_fragment: Optional[str], max_len: int) -> str:
    if not html_fragment:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html_fragment))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]
