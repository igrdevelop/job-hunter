"""Greenhouse job pages — description in HTML; generic extractor is enough."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_ats_greenhouse(url: str) -> str:
    logger.info(f"[ats:greenhouse] fetching {url}")
    return fetch_html(url)
