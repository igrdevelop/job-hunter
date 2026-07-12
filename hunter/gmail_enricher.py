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

import asyncio
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import requests

from hunter.config import (
    GMAIL_ENRICH_CONCURRENCY,
    GMAIL_ENRICH_DOMAIN_DELAY,
    GMAIL_ENRICH_DOMAIN_LIMIT,
    GMAIL_ENRICH_SKIP_HOSTS,
    GMAIL_ENRICH_TIMEOUT,
    PRACUJ_HOST_CONCURRENCY,
    PRACUJ_HOST_DELAY_SEC,
)
from hunter.models import Job
from hunter.rate_limiter import DomainLimiter
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

    resp = requests.get(f"{_DETAIL_API}/{slug}", headers=_JJ_HEADERS, timeout=GMAIL_ENRICH_TIMEOUT)
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
        email_meta=job.email_meta,
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
        email_meta=job.email_meta,
    )


def _is_skipped_host(domain: str) -> bool:
    """True if `domain` is on the enrich skip list (hard-blocking hosts)."""
    return any(h in domain for h in GMAIL_ENRICH_SKIP_HOSTS)


def _enrich_one(job: Job) -> Job:
    domain = (urlparse(job.url).hostname or "").lower()
    # Hosts that hard-block (LinkedIn w/o session, pracuj Cloudflare) only 429 here
    # and poison the shared rate budget — keep the email-derived stub instead.
    if _is_skipped_host(domain):
        logger.info("[gmail_enricher]   → skip-host %r — keeping stub (no fetch)", domain)
        return job
    logger.info("[gmail_enricher] enriching %s", job.url)
    try:
        if "justjoin.it" in domain:
            result = _enrich_justjoin(job)
        elif any(
            d in domain
            for d in (
                "nofluffjobs.com",
                "bulldogjob.com",
                "bulldogjob.pl",
                "pracuj.pl",
                "linkedin.com",
            )
        ):
            result = _enrich_via_text(job)
        else:
            logger.info("[gmail_enricher]   → no enricher for %r — keeping stub", domain)
            return job

        if result is job:
            logger.info("[gmail_enricher]   → unchanged (stub kept): %r", job.title)
        else:
            logger.info(
                "[gmail_enricher]   → enriched: %r @ %r  loc=%r",
                result.title,
                result.company,
                result.location,
            )
        return result
    except Exception as e:
        logger.warning("[gmail_enricher]   → FAILED for %s — %s", job.url, e)
        return job


async def _enrich_jobs_async(jobs: list[Job]) -> list[Job]:
    """Enrich jobs concurrently under global + per-host rate limits.

    pracuj.pl is throttled harder than other hosts so a burst of detail fetches
    doesn't trip Cloudflare's HTTP 429. On any error/timeout the original stub is kept.
    """
    global_sem = asyncio.Semaphore(GMAIL_ENRICH_CONCURRENCY)
    limiter = DomainLimiter(
        GMAIL_ENRICH_DOMAIN_LIMIT,
        GMAIL_ENRICH_DOMAIN_DELAY,
        overrides={"pracuj.pl": (PRACUJ_HOST_CONCURRENCY, PRACUJ_HOST_DELAY_SEC)},
    )

    async def _one(job: Job) -> Job:
        try:
            return await limiter.fetch(
                job.url,
                global_sem,
                lambda _url: _enrich_one(job),
                timeout=GMAIL_ENRICH_TIMEOUT,
            )
        except Exception as e:
            logger.warning("[gmail_enricher] timeout/error for %s — %s", job.url, e)
            return job

    return await asyncio.gather(*[_one(job) for job in jobs])


def enrich_jobs(jobs: list[Job]) -> list[Job]:
    """Enrich Gmail stub jobs with real metadata. Per-host throttled, best-effort.

    Runs the async enrichment to completion. `enrich_jobs` is invoked from
    `GmailSource.search()`, which the hunt loop runs in a worker thread
    (`asyncio.to_thread`), so a fresh event loop here is safe.
    """
    if not jobs:
        return jobs

    logger.info("[gmail_enricher] starting enrichment for %d job(s)", len(jobs))

    enriched = asyncio.run(_enrich_jobs_async(jobs))

    ok = sum(1 for orig, res in zip(jobs, enriched, strict=False) if res is not orig)
    logger.info(
        "[gmail_enricher] done: %d/%d enriched, %d kept as stub",
        ok,
        len(jobs),
        len(jobs) - ok,
    )
    return enriched
