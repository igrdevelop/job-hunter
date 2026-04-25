"""Fetch a Workable-hosted job page → plain text.

Workable serves the description in SSR HTML, so the generic html_fallback
extractor handles it well. If a specific company breaks, switch this to the
JSON detail endpoint:
    GET https://apply.workable.com/api/v3/accounts/{slug}/jobs/{shortcode}
"""

import logging

from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)


def fetch_ats_workable(url: str) -> str:
    logger.info(f"[ats:workable] fetching {url}")
    return fetch_html(url)
