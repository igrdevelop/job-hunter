"""Recruitee career pages (any *.recruitee.com) — HTML via generic extractor."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_ats_recruitee(url: str) -> str:
    logger.info(f"[ats:recruitee] fetching {url}")
    return fetch_html(url)
