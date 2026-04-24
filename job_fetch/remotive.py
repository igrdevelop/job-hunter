"""Fetch Remotive job page by URL → plain text."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_remotive(url: str) -> str:
    logger.info(f"[remotive] fetching {url}")
    return fetch_html(url)
