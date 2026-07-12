"""
Working Nomads — curated remote jobs, served from a public Elasticsearch index.

Strategy: the site exposes its Elasticsearch index ``jobsapi`` directly. We POST
a query to ``/jobsapi/_search`` and read job documents straight from the hits;
each ``_source`` already carries the full HTML description, so ``fetch_text``
re-queries the same index by slug instead of scraping the SPA job page.

Listing URL (canonical, used for dedup + Telegram): https://www.workingnomads.com/jobs/{slug}

Note: ``apply_url`` in each document points at the employer's real ATS — we keep
the Working Nomads page as the canonical URL so dedup stays inside one domain.
"""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import REMOTE_ANY, ensure_remote_token, strip_html

logger = logging.getLogger(__name__)

SEARCH_URL = "https://www.workingnomads.com/jobsapi/_search"
JOB_URL_TMPL = "https://www.workingnomads.com/jobs/{slug}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
}
TIMEOUT = 30
MAX_RESULTS = 100

# OR-matched against the job TITLE. Mirrors FILTER["title_keywords"] so the rows
# we pull are the ones the central filter will actually keep (it requires a
# frontend keyword in the title). A broad multi_match over the description pulled
# mostly generic "Software Engineer" rows that the central title filter dropped.
TITLE_TERMS = "angular frontend front-end javascript typescript"


class WorkingNomadsSource(BaseSource):
    name = "workingnomads"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "workingnomads.com" in host

    def search(self) -> list[Job]:
        try:
            hits = self._fetch()
        except Exception as e:
            logger.warning(f"[workingnomads] search failed: {e}")
            return []

        logger.info(f"[workingnomads] _search returned {len(hits)} raw hits")
        seen_urls: set[str] = set()
        jobs: list[Job] = []
        for raw in hits:
            job = self._parse(raw)
            if not job or job.url in seen_urls:
                continue
            if not self.matches_coarse_prefilter(job.title, _prefilter_context(raw)):
                continue
            seen_urls.add(job.url)
            jobs.append(job)

        logger.info(f"[workingnomads] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch(self) -> list[dict[str, Any]]:
        query = {
            "size": MAX_RESULTS,
            "sort": [{"pub_date": {"order": "desc"}}],
            "query": {
                "bool": {
                    "must": [{"match": {"title": {"query": TITLE_TERMS, "operator": "or"}}}],
                    "filter": [{"term": {"expired": False}}],
                }
            },
        }
        resp = requests.post(SEARCH_URL, headers=HEADERS, json=query, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        return [h.get("_source", {}) for h in data.get("hits", {}).get("hits", [])]

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company") or "").strip()
        slug = (raw.get("slug") or "").strip()
        if not title or not company or not slug:
            return None
        return Job(
            title=title,
            company=company,
            location=_format_location(raw.get("locations")),
            salary=(raw.get("salary_range_short") or "").strip() or None,
            url=JOB_URL_TMPL.format(slug=slug),
            source=self.name,
            raw=raw,
        )

    def fetch_text(self, url: str) -> str:
        """Re-query the index by slug and return the stored description as text.

        Falls back to generic HTML extraction if the slug lookup fails or the
        document carries no description.
        """
        slug = _slug_from_url(url)
        if slug:
            try:
                desc = self._fetch_description(slug)
                if desc:
                    return desc
            except Exception as e:
                logger.warning(f"[workingnomads] slug lookup failed ({e}), using html_fallback")
        from hunter.sources.html_fallback import fetch_html

        return fetch_html(url)

    def _fetch_description(self, slug: str) -> str:
        query = {
            "size": 5,
            "query": {"match": {"slug": slug}},
        }
        resp = requests.post(SEARCH_URL, headers=HEADERS, json=query, timeout=TIMEOUT)
        resp.raise_for_status()
        for hit in resp.json().get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            if (src.get("slug") or "").strip() == slug:
                return strip_html(src.get("description"), 20000)
        return ""


def _slug_from_url(url: str) -> str:
    path = urlparse(url).path.rstrip("/")
    if "/jobs/" in path:
        return path.rsplit("/jobs/", 1)[-1]
    return ""


def _format_location(locations: Any) -> str:
    if not isinstance(locations, list):
        locations = [locations] if locations else []
    parts = [str(p).strip() for p in locations if p and str(p).strip()]
    if not parts:
        return "Remote"
    if all(p.lower() in REMOTE_ANY for p in parts):
        return "Remote"
    return ensure_remote_token(", ".join(parts))


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    cat = raw.get("category_name")
    if isinstance(cat, str) and cat.strip():
        parts.append(cat.strip())
    for key in ("tags", "all_tags"):
        vals = raw.get(key)
        if isinstance(vals, list):
            parts.extend(str(v) for v in vals if v)
    desc = raw.get("description")
    if isinstance(desc, str) and desc:
        parts.append(strip_html(desc, 1200))
    return " ".join(parts)
