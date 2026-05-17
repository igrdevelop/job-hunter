"""
job_fetch/linkedin.py — Fetch a LinkedIn job posting via Playwright with saved session.

Requires:
  pip install playwright
  playwright install chromium

Session file is set via LINKEDIN_STORAGE_STATE in .env
(e.g. D:/LearningProject/Claude/.secrets/linkedin_storage_state.json)

If session file is not configured, raises a clear error so the caller can log it
and move on rather than silently failing.
"""

import logging
import os
import re
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

_ENV_KEY = "LINKEDIN_STORAGE_STATE"
_TIMEOUT_MS = 20_000  # 20 sec page load timeout
_MAX_TEXT_LEN = 15_000


def _get_storage_state_path() -> Path | None:
    val = os.environ.get(_ENV_KEY, "").strip()
    if not val:
        return None
    p = Path(val)
    return p if p.exists() else None


def fetch_linkedin(url: str) -> str:
    """Fetch LinkedIn job view page text using Playwright with saved session.

    Raises RuntimeError if session not configured or page returns login wall.
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        logger.warning(
            "[linkedin] playwright not installed — falling back to HTML fetch. "
            "Install with: pip install playwright && playwright install chromium"
        )
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    storage_state = _get_storage_state_path()
    if not storage_state:
        logger.warning(
            f"[linkedin] {_ENV_KEY} not set — falling back to HTML fetch. "
            f"Run python tools/linkedin_login.py to enable full session fetch."
        )
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    logger.info(f"[linkedin] Fetching {url} with session from {storage_state}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            storage_state=str(storage_state),
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()

        try:
            page.goto(url, timeout=_TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            raise RuntimeError(f"LinkedIn page timed out: {url}")

        # Detect login wall
        current = page.url
        if "linkedin.com/login" in current or "linkedin.com/checkpoint" in current:
            browser.close()
            raise RuntimeError(
                f"LinkedIn redirected to login page — session expired.\n"
                f"Re-run: python tools/linkedin_login.py  to refresh storage_state."
            )

        # Wait for job description to appear
        try:
            page.wait_for_selector(
                ".jobs-description, .job-view-layout, .description__text",
                timeout=10_000,
            )
        except PWTimeout:
            pass  # Page may have loaded differently — try extracting anyway

        # Extract visible text
        text = page.evaluate("""() => {
            // Remove scripts, styles and nav clutter
            const remove = ['script','style','nav','footer','header','noscript'];
            remove.forEach(t => document.querySelectorAll(t).forEach(e => e.remove()));
            return document.body ? document.body.innerText : '';
        }""")

        browser.close()

    text = _clean(text)
    if len(text) < 100:
        raise RuntimeError(f"LinkedIn page returned too little text ({len(text)} chars) for {url}")

    if len(text) > _MAX_TEXT_LEN:
        text = text[:_MAX_TEXT_LEN] + "\n\n[... truncated ...]"

    logger.info(f"[linkedin] Got {len(text)} chars")
    return text


def _clean(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()
