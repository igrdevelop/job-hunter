"""
job_fetch — Fetch job posting text from a URL.

Usage:
    from job_fetch import fetch_job_text
    text = fetch_job_text("https://justjoin.it/job-offer/some-slug")
"""

import logging
from urllib.parse import urlparse

from job_fetch.justjoin import fetch_justjoin
from job_fetch.nofluffjobs import fetch_nofluffjobs
from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_job_text(url: str) -> str:
    """Fetch and return plain-text job posting from the given URL.

    Dispatches to a source-specific fetcher when possible,
    falls back to generic HTML extraction.
    Raises on failure (caller decides how to handle).
    """
    domain = (urlparse(url).hostname or "").lower()

    if "justjoin.it" in domain:
        logger.info(f"[job_fetch] JustJoin detected: {url}")
        return fetch_justjoin(url)

    if "nofluffjobs.com" in domain:
        logger.info(f"[job_fetch] NoFluffJobs detected: {url}")
        return fetch_nofluffjobs(url)

    if "linkedin.com" in domain:
        logger.info(f"[job_fetch] LinkedIn detected: {url}")
        from job_fetch.linkedin import fetch_linkedin
        return fetch_linkedin(url)

    if "bulldogjob.com" in domain:
        logger.info(f"[job_fetch] Bulldogjob detected: {url}")
        from job_fetch.bulldogjob import fetch_bulldogjob
        return fetch_bulldogjob(url)

    if "pracuj.pl" in domain:
        logger.info(f"[job_fetch] Pracuj.pl detected: {url}")
        from job_fetch.pracuj import fetch_pracuj
        return fetch_pracuj(url)

    logger.info(f"[job_fetch] Generic HTML fetch: {url}")
    return fetch_html(url)
