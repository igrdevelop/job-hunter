"""Fetch We Work Remotely job page by URL → plain text via generic HTML extraction."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_weworkremotely(url: str) -> str:
    logger.info(f"[weworkremotely] fetching {url}")
    return fetch_html(url)
