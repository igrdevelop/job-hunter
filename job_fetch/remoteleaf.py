"""Fetch RemoteLeaf job detail page → plain text via generic HTML extraction."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_remoteleaf(url: str) -> str:
    logger.info(f"[remoteleaf] fetching {url}")
    return fetch_html(url)
