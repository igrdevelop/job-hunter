"""Ashby job pages (jobs.ashbyhq.com) — HTML via generic extractor."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_ats_ashby(url: str) -> str:
    logger.info(f"[ats:ashby] fetching {url}")
    return fetch_html(url)
