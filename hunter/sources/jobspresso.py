"""
Jobspresso — curated remote jobs (WP Job Manager site), public RSS feed.

Feed: https://jobspresso.co/?feed=job_feed

The feed returns only the ~10 most recent listings (no server-side category
filter or pagination), so this is a low-volume trickle source. Each <item>
carries WP Job Manager custom fields (company, location, job_type) in addition
to the standard title/link/description. We pre-filter on title + those fields.
"""

from __future__ import annotations

import logging
from html import unescape
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.sources.text_utils import REMOTE_ANY, ensure_remote_token, strip_html

logger = logging.getLogger(__name__)

RSS_URL = "https://jobspresso.co/?feed=job_feed"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
TIMEOUT = 30


class JobspressoSource(BaseSource):
    name = "jobspresso"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "jobspresso.co" in host

    def search(self) -> list[Job]:
        raw_items = self._fetch_rss()
        logger.info(f"[jobspresso] RSS returned {len(raw_items)} total items")

        seen_urls: set[str] = set()
        jobs: list[Job] = []
        for raw in raw_items:
            job = self._parse(raw)
            if not job or job.url in seen_urls:
                continue
            if not self.matches_coarse_prefilter(job.title, _prefilter_context(raw)):
                continue
            seen_urls.add(job.url)
            jobs.append(job)

        logger.info(f"[jobspresso] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_rss(self) -> list[dict]:
        try:
            resp = requests.get(RSS_URL, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[jobspresso] RSS fetch failed: {e}")
            return []
        return parse_jobspresso_rss_xml(resp.text)

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not url:
            return None
        company = (raw.get("company") or "").strip() or "Unknown"
        return Job(
            title=title,
            company=company,
            location=_format_location(raw.get("location")),
            salary=None,
            url=url,
            source=self.name,
            raw=raw,
        )


def _local(tag: str) -> str:
    """Strip the XML namespace from an ElementTree tag → bare local name."""
    return tag.split("}", 1)[-1]


def parse_jobspresso_rss_xml(xml_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        logger.error(f"[jobspresso] RSS parse error: {e}")
        return []

    results: list[dict] = []
    for item in root.iter("item"):
        fields: dict[str, str] = {}
        for child in item:
            name = _local(child.tag)
            if name in fields:  # first occurrence wins (e.g. guid vs link)
                continue
            fields[name] = (child.text or "").strip()

        title = unescape(fields.get("title", ""))
        link = fields.get("link", "")
        if not title or not link:
            continue

        results.append(
            {
                "title": title,
                "url": link,
                "company": unescape(fields.get("company", "")),
                "location": unescape(fields.get("location", "")),
                "job_type": unescape(fields.get("job_type", "")),
                "job_category": unescape(fields.get("job_category", "")),
                "description_html": fields.get("description", ""),
            }
        )

    return results


def _format_location(loc: Optional[str]) -> str:
    """Jobspresso is remote-only; ensure the 'remote' token survives the central
    location whitelist while keeping the geographic restriction as a hint."""
    loc = (loc or "").strip()
    if not loc or loc.lower() in REMOTE_ANY:
        return "Remote"
    return ensure_remote_token(loc)


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    for key in ("job_type", "job_category", "location"):
        val = raw.get(key)
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
    desc = raw.get("description_html")
    if isinstance(desc, str) and desc:
        parts.append(strip_html(desc, 1200))
    return " ".join(parts)
