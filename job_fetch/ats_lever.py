"""Lever-hosted job pages — HTML description via generic extractor."""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_ats_lever(url: str) -> str:
    logger.info(f"[ats:lever] fetching {url}")
    return fetch_html(url)
