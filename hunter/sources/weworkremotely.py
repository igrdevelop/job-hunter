"""
We Work Remotely — public RSS feed of all live remote listings.

Feed: https://weworkremotely.com/remote-jobs.rss

Item titles are usually "Company: Job title". We pre-filter using plain text
from the HTML description plus category and skills.
"""

from __future__ import annotations

import logging
import re
from html import unescape
from typing import Optional
from xml.etree import ElementTree

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

RSS_URL = "https://weworkremotely.com/remote-jobs.rss"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
TIMEOUT = 60

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)


class WeworkremotelySource(BaseSource):
    name = "weworkremotely"

    def search(self) -> list[Job]:
        raw_items = self._fetch_rss()
        logger.info(f"[weworkremotely] RSS returned {len(raw_items)} total items")

        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for raw in raw_items:
            job = self._parse(raw)
            if not job or job.url in seen_urls:
                continue
            ctx = _prefilter_context(raw)
            if not self.matches_coarse_prefilter(job.title, ctx):
                continue
            seen_urls.add(job.url)
            jobs.append(job)

        logger.info(f"[weworkremotely] {len(jobs)} jobs after pre-filter")
        return jobs

    def _fetch_rss(self) -> list[dict]:
        try:
            resp = requests.get(RSS_URL, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[weworkremotely] RSS fetch failed: {e}")
            return []

        return parse_weworkremotely_rss_xml(resp.text)

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        url = (raw.get("url") or "").strip()
        company = (raw.get("company") or "").strip()
        if not title or not url:
            return None
        if not company:
            company = "Unknown"
        location = (raw.get("location") or "").strip() or "Remote"
        return Job(
            title=title,
            company=company,
            location=location,
            salary=None,
            url=url,
            source=self.name,
            raw=raw,
        )


def _el_text(parent: ElementTree.Element, tag: str) -> str:
    el = parent.find(tag)
    if el is not None and el.text:
        return unescape(el.text.strip())
    return ""


def parse_weworkremotely_rss_xml(xml_text: str) -> list[dict]:
    try:
        root = ElementTree.fromstring(xml_text)
    except ElementTree.ParseError as e:
        logger.error(f"[weworkremotely] RSS parse error: {e}")
        return []

    results: list[dict] = []
    for item in root.iter("item"):
        raw_title = _el_text(item, "title")
        link = _el_text(item, "link")
        if not raw_title or not link:
            continue

        company, job_title = _split_company_title(raw_title)
        region = _el_text(item, "region")
        country = _el_text(item, "country")
        state = _el_text(item, "state")
        category = _el_text(item, "category")
        skills = _el_text(item, "skills")
        desc_html = _el_text(item, "description")

        location = _format_location(region, state, country)

        results.append(
            {
                "title": job_title,
                "company": company,
                "location": location,
                "url": link,
                "category": category,
                "skills": skills,
                "description_html": desc_html,
            }
        )

    return results


def _split_company_title(raw_title: str) -> tuple[str, str]:
    t = raw_title.strip()
    if ": " in t:
        company, role = t.split(": ", 1)
        company = company.strip()
        role = role.strip()
        if company and role:
            return company, role
    return "Unknown", t


def _format_location(region: str, state: str, country: str) -> str:
    parts = [p for p in (region, state, country) if p and p.strip()]
    if not parts:
        return "Remote"
    return ", ".join(parts)


def _html_to_plain(html_fragment: str, max_len: int) -> str:
    if not html_fragment:
        return ""
    text = unescape(_HTML_TAG_RE.sub(" ", html_fragment))
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    cat = raw.get("category")
    if isinstance(cat, str) and cat.strip():
        parts.append(cat.strip())
    skills = raw.get("skills")
    if isinstance(skills, str) and skills.strip():
        parts.append(skills.strip())
    desc = raw.get("description_html")
    if isinstance(desc, str) and desc:
        parts.append(_html_to_plain(desc, 1200))
    return " ".join(parts)
