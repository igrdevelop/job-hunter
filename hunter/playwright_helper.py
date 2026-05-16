"""Shared headless Chromium fetch helper for Cloudflare-protected sites."""

import logging
from contextlib import contextmanager
from typing import Iterator

logger = logging.getLogger(__name__)

BROWSER_TIMEOUT = 30_000  # ms

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Extra args for low-memory / Docker environments
_BROWSER_ARGS = ["--no-sandbox", "--disable-dev-shm-usage"]


@contextmanager
def chromium_page(url: str, wait_until: str = "networkidle") -> Iterator:
    """Launch headless Chromium, navigate to url, yield the Page object.

    Raises ImportError if playwright is not installed.
    Raises playwright.sync_api.TimeoutError / Error on navigation failure.

    Usage:
        with chromium_page(url) as page:
            html = page.content()
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, args=_BROWSER_ARGS)
        try:
            page = browser.new_page(user_agent=_CHROME_UA)
            page.goto(url, wait_until=wait_until, timeout=BROWSER_TIMEOUT)
            yield page
        finally:
            browser.close()
