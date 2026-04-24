"""Fetch Remote OK job page by URL → plain text."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_remoteok(url: str) -> str:
    logger.info(f"[remoteok] fetching {url}")
    return fetch_html(url)
