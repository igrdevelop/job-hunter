"""Fetch Arbeitnow job page by URL → plain text (HTML listing on site)."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_arbeitnow(url: str) -> str:
    """Fetch Arbeitnow job detail page and return visible text."""
    logger.info(f"[arbeitnow] fetching {url}")
    return fetch_html(url)
