"""
Gmail Job Enricher — fetches real title/company/location/salary
from job URLs extracted from alert emails.

Per-source strategies:
  JustJoin    → GET /api/candidate-api/offers/{slug}  (same JSON as listing API)
  NoFluffJobs → _enrich_via_text (job_fetch returns structured "Job Title: ..." text)
  Bulldogjob  → _enrich_via_text (job_fetch HTML parse)
  Pracuj      → _enrich_via_text (job_fetch __NEXT_DATA__ parse)
  LinkedIn    → _enrich_via_text (Playwright; silently skips if unavailable)

Fallback: on any error the original stub Job is returned unchanged.
Dedup still works because URL is the canonical key regardless of stub title.
"""

import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse

import requests

from hunter.config import GMAIL_ENRICH_CONCURRENCY, GMAIL_ENRICH_TIMEOUT
from hunter.models import Job
from hunter.sources.justjoin import JustJoinSource

logger = logging.getLogger(__name__)

_DETAIL_API = "https://justjoin.it/api/candidate-api/offers"
_JJ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://justjoin.it/",
}


def _enrich_justjoin(job: Job) -> Job:
    match = re.search(r"/(?:job-offer|offers)/([a-z0-9-]+)", job.url)
    if not match:
        return job
    slug = match.group(1)

    resp = requests.get(
        f"{_DETAIL_API}/{slug}", headers=_JJ_HEADERS, timeout=GMAIL_ENRICH_TIMEOUT
    )
    if resp.status_code == 404:
        return job
    resp.raise_for_status()
    offer = resp.json()

    title = (offer.get("title") or "").strip()
    company = (offer.get("companyName") or "").strip()
    if not title or not company:
        return job

    # workplaceType from offer; page_context not available here — derive from offer
    workplace = (offer.get("workplaceType") or "").lower()
    page_context = "remote" if workplace == "remote" else "office"
    location = JustJoinSource._parse_location(offer, page_context)
    salary = JustJoinSource._parse_salary(offer)

    return Job(
        title=title,
        company=company,
        location=location,
        salary=salary,
        url=job.url,
        source=job.source,
        raw=offer,
    )


def _enrich_via_text(job: Job) -> Job:
    """Generic enricher: calls the source dispatcher, parses structured header lines."""
    from hunter.sources import fetch_job_text

    text = fetch_job_text(job.url)

    def _extract(label: str) -> Optional[str]:
        m = re.search(rf"^{label}:\s*(.+)", text, re.MULTILINE | re.IGNORECASE)
        return m.group(1).strip() if m else None

    title = _extract("Job Title") or job.title
    company = _extract("Company") or job.company
    location = _extract("Location") or job.location
    salary = _extract("Salary") or job.salary

    if title == job.title and company == job.company:
        return job

    return Job(
        title=title,
        company=company,
        location=location or job.location,
        salary=salary,
        url=job.url,
        source=job.source,
        raw=job.raw,
    )


def _enrich_one(job: Job) -> Job:
    domain = (urlparse(job.url).hostname or "").lower()
    logger.info("[gmail_enricher] enriching %s", job.url)
    try:
        if "justjoin.it" in domain:
            result = _enrich_justjoin(job)
        elif any(d in domain for d in ("nofluffjobs.com", "bulldogjob.com", "bulldogjob.pl", "pracuj.pl", "linkedin.com")):
            result = _enrich_via_text(job)
        else:
            logger.info("[gmail_enricher]   → no enricher for %r — keeping stub", domain)
            return job

        if result is job:
            logger.info("[gmail_enricher]   → unchanged (stub kept): %r", job.title)
        else:
            logger.info(
                "[gmail_enricher]   → enriched: %r @ %r  loc=%r",
                result.title, result.company, result.location,
            )
        return result
    except Exception as e:
        logger.warning("[gmail_enricher]   → FAILED for %s — %s", job.url, e)
        return job


def enrich_jobs(jobs: list[Job]) -> list[Job]:
    """Enrich Gmail stub jobs with real metadata. Thread-parallel, best-effort."""
    if not jobs:
        return jobs

    logger.info("[gmail_enricher] starting enrichment for %d job(s)", len(jobs))

    enriched: dict[str, Job] = {}
    with ThreadPoolExecutor(max_workers=GMAIL_ENRICH_CONCURRENCY) as pool:
        future_to_url = {pool.submit(_enrich_one, job): job.url for job in jobs}
        for future in as_completed(future_to_url, timeout=GMAIL_ENRICH_TIMEOUT * 3):
            url = future_to_url[future]
            try:
                result = future.result(timeout=GMAIL_ENRICH_TIMEOUT)
                enriched[url] = result
            except Exception as e:
                logger.warning("[gmail_enricher] timeout/error for %s — %s", url, e)
                for j in jobs:
                    if j.url == url:
                        enriched[url] = j
                        break

    ok = sum(1 for j in jobs if enriched.get(j.url, j) is not j)
    logger.info("[gmail_enricher] done: %d/%d enriched, %d kept as stub", ok, len(jobs), len(jobs) - ok)
    return [enriched.get(j.url, j) for j in jobs]
