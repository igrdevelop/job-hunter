"""
NoFluffJobs source.

API: POST https://nofluffjobs.com/api/search/posting
     ?salaryCurrency=PLN&salaryPeriod=month&region=pl
Body: {"criteriaSearch": {"category": ["frontend"]}, "page": 1}

Returns up to 265+ frontend jobs. We run two searches:
  1. All frontend jobs (global filter handles Wroclaw/Remote)
  2. Remote-only (extra coverage)

Tested 2026-04: endpoint works without auth.
"""

import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

API_URL = "https://nofluffjobs.com/api/search/posting"
API_PARAMS = {
    "salaryCurrency": "PLN",
    "salaryPeriod": "month",
    "region": "pl",
}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Origin": "https://nofluffjobs.com",
    "Referer": "https://nofluffjobs.com/",
}
TIMEOUT = 20
JOB_BASE_URL = "https://nofluffjobs.com/pl/job"

POSTING_API = "https://nofluffjobs.com/api/posting"
POSTING_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://nofluffjobs.com/",
}


def _extract_posting_slug(url: str) -> str:
    match = re.search(r"/job/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Cannot extract NoFluffJobs slug from URL: {url}")
    return match.group(1)


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dig(data: dict, path: str):
    """Walk a dotted path through nested dicts; return None if any hop is missing."""
    cur = data
    for part in path.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


def _first(data: dict, paths: list[str]):
    """Return the first non-empty value among several candidate dotted paths."""
    for path in paths:
        val = _dig(data, path)
        if val:
            return val
    return None


def _coerce_text(value) -> str:
    """Render an HTML string, a list of strings, or a dict of strings as plain text."""
    if not value:
        return ""
    if isinstance(value, str):
        return _strip_html(value)
    if isinstance(value, list):
        lines = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("value") or item.get("text") or ""
            text = _strip_html(str(item)).strip()
            if text:
                lines.append(f"- {text}")
        return "\n".join(lines)
    if isinstance(value, dict):
        # join string-ish fields (e.g. details: {quote, position, description})
        chunks = [_strip_html(str(v)) for v in value.values() if isinstance(v, str) and v.strip()]
        return "\n".join(c for c in chunks if c)
    return _strip_html(str(value))


def _extract_company(data: dict) -> str:
    return (
        data.get("name") or _dig(data, "company.name") or _dig(data, "company.companyName") or "N/A"
    )


def _extract_seniority(data: dict) -> list[str]:
    sen = data.get("seniority") or _dig(data, "basics.seniority") or []
    if isinstance(sen, str):
        return [sen]
    return [str(s) for s in sen if s]


def _extract_salary(data: dict):
    """Return (low, high, currency, emp_type) across old and new schemas, or None."""
    ess = data.get("essentials") or {}

    # Old schema: essentials.salary
    sal = ess.get("salary") or {}
    if sal.get("from") or sal.get("to"):
        return sal.get("from"), sal.get("to"), sal.get("currency", "PLN"), sal.get("type", "")

    # New schema: essentials.originalSalary.types.<empType>.range = [low, high]
    orig = ess.get("originalSalary") or {}
    cur = orig.get("currency", "PLN")
    for emp_type, info in (orig.get("types") or {}).items():
        rng = (info or {}).get("range") or []
        if rng:
            low = rng[0] if len(rng) > 0 else None
            high = rng[1] if len(rng) > 1 else None
            if low or high:
                return low, high, cur, emp_type
    return None


# (label, candidate dotted paths) — first non-empty path wins. Covers the current
# NoFluffJobs schema (details/requirements.description, specs.dailyTasks) plus the
# legacy `sections.*` shape so older cached/alternate payloads still parse.
_SECTION_SPECS: list[tuple[str, list[str]]] = [
    ("Description", ["details.description", "sections.description", "description"]),
    ("Requirements", ["requirements.description", "sections.requirements"]),
    ("Responsibilities", ["specs.dailyTasks", "sections.responsibilities", "responsibilities"]),
    ("Methodology", ["specs.methodology", "sections.methodology", "methodology"]),
    ("Environment", ["specs.environment", "sections.environment", "environment"]),
]


def _format_posting_text(data: dict) -> str:
    parts: list[str] = [
        f"Job Title: {data.get('title', 'N/A')}",
        f"Company: {_extract_company(data)}",
    ]

    location = data.get("location") or {}
    places = location.get("places") or []
    remote = data.get("fullyRemote", False)
    loc_str = "Remote" if remote else ", ".join(p.get("city", "") for p in places if p.get("city"))
    parts.append(f"Location: {loc_str or 'N/A'}")

    seniority = _extract_seniority(data)
    if seniority:
        parts.append(f"Seniority: {', '.join(seniority)}")

    musts = _dig(data, "requirements.musts") or []
    nices = _dig(data, "requirements.nices") or []
    if musts:
        parts.append(f"Must-have: {', '.join(m.get('value', '') for m in musts)}")
    if nices:
        parts.append(f"Nice-to-have: {', '.join(n.get('value', '') for n in nices)}")

    salary = _extract_salary(data)
    if salary:
        low, high, cur, emp = salary
        parts.append(f"Salary: {low or '?'}–{high or '?'} {cur} {emp}".rstrip())

    seen_blocks: set[str] = set()
    for label, paths in _SECTION_SPECS:
        content = _coerce_text(_first(data, paths))
        if content and content not in seen_blocks:
            seen_blocks.add(content)
            parts.append(f"\n--- {label} ---\n{content}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError("NoFluffJobs posting returned almost no content")
    return text


class NoFluffJobsSource(BaseSource):
    name = "nofluffjobs"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "nofluffjobs.com" in host

    def fetch_text(self, url: str) -> str:
        """Try the posting detail API first, fall back to generic HTML extraction."""
        slug = _extract_posting_slug(url)
        try:
            resp = requests.get(f"{POSTING_API}/{slug}", headers=POSTING_HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
            data = resp.json()
            return _format_posting_text(data)
        except Exception:
            from hunter.sources.html_fallback import fetch_html

            return fetch_html(url)

    def search(self) -> list[Job]:
        all_jobs: list[Job] = []

        for body in self._search_bodies():
            try:
                all_jobs.extend(self._fetch(body))
            except Exception as e:
                logger.error(f"[NoFluffJobs] fetch error: {e}")

        # Deduplicate by (title + company): NoFluffJobs creates a separate
        # URL per region for the same job (e.g. polcode-remote, polcode-lodz).
        # We keep only the first occurrence — usually the remote/main one.
        seen: set[str] = set()
        jobs: list[Job] = []
        for job in all_jobs:
            key = f"{job.title.lower()}|{job.company.lower()}"
            if key not in seen:
                seen.add(key)
                jobs.append(job)

        logger.info(f"[NoFluffJobs] {len(all_jobs)} raw → {len(jobs)} after dedup by title+company")
        return jobs

    # ── Search request bodies ─────────────────────────────────────────────────

    def _search_bodies(self) -> list[dict]:
        """Two queries: all frontend + remote-only for better coverage."""
        return [
            {
                "criteriaSearch": {"category": ["frontend"]},
                "page": 1,
            },
            {
                "criteriaSearch": {"category": ["frontend"], "requirement": ["remote"]},
                "page": 1,
            },
        ]

    # ── HTTP fetch ────────────────────────────────────────────────────────────

    def _fetch(self, body: dict) -> list[Job]:
        resp = requests.post(
            API_URL,
            params=API_PARAMS,
            json=body,
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()

        postings = data.get("postings", [])
        logger.info(f"[NoFluffJobs] fetched {len(postings)} raw postings")

        jobs = []
        for posting in postings:
            job = self._parse(posting)
            if job:
                jobs.append(job)
        return jobs

    # ── Parser ────────────────────────────────────────────────────────────────

    def _parse(self, p: dict) -> Optional[Job]:
        title = (p.get("title") or "").strip()
        company = (p.get("name") or "").strip()
        slug = (p.get("url") or "").strip()

        if not title or not company or not slug:
            return None

        url = f"{JOB_BASE_URL}/{slug}"
        location = self._parse_location(p)
        salary = self._parse_salary(p)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=p,
        )

    @staticmethod
    def _parse_location(p: dict) -> str:
        fully_remote = p.get("fullyRemote", False)
        places = (p.get("location") or {}).get("places", [])

        if fully_remote and not places:
            return "Remote"

        cities = [pl.get("city", "") for pl in places if pl.get("city")]
        city = cities[0] if cities else ""

        if fully_remote:
            return f"{city} (Remote)" if city else "Remote"
        if city:
            return city  # global filter checks for wroclaw/hybrid/remote in location
        return "Unknown"

    @staticmethod
    def _parse_salary(p: dict) -> Optional[str]:
        sal = p.get("salary")
        if not sal:
            return None

        low = sal.get("from")
        high = sal.get("to")
        currency = (sal.get("currency") or "PLN").upper()
        emp_type = (sal.get("type") or "").upper()

        if low or high:
            if low and high:
                amount = f"{int(low):,}–{int(high):,}".replace(",", " ")
            elif low:
                amount = f"{int(low):,}+".replace(",", " ")
            else:
                amount = f"up to {int(high):,}".replace(",", " ")
            label = f"{amount} {currency}"
            if emp_type:
                label += f" {emp_type}"
            return label

        return None
