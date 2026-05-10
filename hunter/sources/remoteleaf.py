"""
RemoteLeaf — server-rendered job listing HTML (Django + HTMX).

No public JSON API for listings; we fetch category pages with ?skills= filters
and paginate via &page=N. Job cards use /company/{company}/{job-slug}/ links.

Listing URLs are aligned with hunter FILTER (frontend / Angular stack).
"""

from __future__ import annotations

import logging
import time
from html import unescape
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://remoteleaf.com"

# Same role bucket as the site’s “Frontend Engineer / Web Developer” filter; skills narrow results.
LISTING_BASE_URLS: tuple[str, ...] = (
    f"{BASE}/jobs/full-time-frontend-engineer-web-developer/?skills=angular",
    f"{BASE}/jobs/full-time-frontend-engineer-web-developer/?skills=typescript",
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 60
MAX_PAGES_PER_LISTING = 6
REQUEST_DELAY_SEC = 0.55


class RemoteleafSource(BaseSource):
    name = "remoteleaf"

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for base_url in LISTING_BASE_URLS:
            page = 1
            while page <= MAX_PAGES_PER_LISTING:
                if page > 1:
                    time.sleep(REQUEST_DELAY_SEC)
                url = _page_url(base_url, page)
                try:
                    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
                    resp.raise_for_status()
                except Exception as e:
                    logger.warning(f"[remoteleaf] fetch failed {url}: {e}")
                    break
                batch = parse_job_cards_from_html(resp.text, page_url=url)
                if not batch:
                    break
                new_in_page = 0
                for raw in batch:
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    ctx = _prefilter_context(raw)
                    if not self.matches_coarse_prefilter(job.title, ctx):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
                    new_in_page += 1
                logger.info(
                    f"[remoteleaf] {url} -> {len(batch)} cards, +{new_in_page} new "
                    f"(total {len(jobs)})"
                )
                if new_in_page == 0:
                    break
                page += 1

        logger.info(f"[remoteleaf] {len(jobs)} jobs after pre-filter")
        return jobs

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        company = (raw.get("company") or "").strip()
        url = (raw.get("url") or "").strip()
        if not title or not company or not url:
            return None
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


def _page_url(base_url: str, page: int) -> str:
    if page <= 1:
        return base_url
    sep = "&" if "?" in base_url else "?"
    return f"{base_url}{sep}page={page}"


def _path_parts(href: str) -> list[str]:
    h = href.split("?", 1)[0].strip("/")
    return [p for p in h.split("/") if p]


def _is_job_company_href(href: str) -> bool:
    parts = _path_parts(href)
    return len(parts) == 3 and parts[0] == "company"


def _is_company_only_href(href: str) -> bool:
    parts = _path_parts(href)
    return len(parts) == 2 and parts[0] == "company"


def parse_job_cards_from_html(html: str, page_url: str = "") -> list[dict]:
    """Extract job rows from a RemoteLeaf listing HTML page (for tests and production)."""
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict] = []
    # Real job cards use `group relative`; skeleton loaders use `animate-pulse` without job links.
    for card in soup.select("#job-results div.card.group.relative"):
        row = _parse_card(card, page_url=page_url)
        if row:
            results.append(row)
    return results


def _parse_card(card: BeautifulSoup, page_url: str) -> Optional[dict]:
    job_a = card.select_one("h3 a[href^='/company/']")
    if not job_a:
        return None
    href = job_a.get("href", "").strip()
    if not _is_job_company_href(href):
        return None
    title = unescape(job_a.get_text(strip=True))
    if not title:
        return None

    company = ""
    for a in card.select("a[href^='/company/']"):
        h = a.get("href", "").strip()
        if _is_company_only_href(h):
            ct = unescape(a.get_text(strip=True))
            if ct:
                company = ct
                break
    if not company:
        return None

    locs: list[str] = []
    for pill in card.select('a[href^="/jobs/in-"]'):
        sp = pill.select_one("span.text-sm.font-medium")
        if sp:
            t = sp.get_text(strip=True)
            if t:
                locs.append(t)
    location = ", ".join(locs) if locs else "Remote"

    summary_el = card.find(
        "p",
        class_=lambda c: isinstance(c, list) and "line-clamp-2" in " ".join(c),
    )
    if summary_el is None:
        summary_el = card.select_one("p.text-base-content")
    summary = ""
    if summary_el:
        summary = unescape(summary_el.get_text(" ", strip=True))

    skills: list[str] = []
    for sp in card.find_all("span", class_=True):
        cls = sp.get("class") or []
        if not isinstance(cls, list):
            cls = [str(cls)]
        if "border-base-300" in cls:
            t = sp.get_text(strip=True)
            if t:
                skills.append(t)

    abs_url = urljoin(BASE, href)
    return {
        "title": title,
        "company": company,
        "location": location,
        "url": abs_url,
        "summary": summary,
        "skills": skills,
        "_listing_page": page_url,
    }


def _prefilter_context(raw: dict) -> str:
    parts: list[str] = []
    s = raw.get("summary")
    if isinstance(s, str) and s:
        parts.append(s)
    skills = raw.get("skills")
    if isinstance(skills, list):
        parts.append(" ".join(str(x) for x in skills))
    return " ".join(parts)
