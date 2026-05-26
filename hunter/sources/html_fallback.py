"""Generic HTML job-page fetcher — default fallback for BaseSource.fetch_text.

Used both as the default implementation in BaseSource and as a last-resort
fallback when no source matches a URL.
"""

import logging
import re
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,pl;q=0.8",
}
TIMEOUT = 25
MAX_TEXT_LEN = 15_000

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id",
    "fbclid", "gclid", "campaignid", "adgroupid",
    "ref", "refId", "trackingId", "trk",
    "sendid", "send_date", "sug",
    "originToLandingJobPostings", "origin",
}


def clean_url(url: str) -> str:
    """Strip tracking/UTM params before fetching — prevents Cloudflare false positives."""
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=False)
    clean = {k: v for k, v in qs.items() if k not in _TRACKING_PARAMS}
    return urlunparse(p._replace(query=urlencode(clean, doseq=True)))


def fetch_html(url: str) -> str:
    """Fetch URL, extract visible text via BeautifulSoup (or regex fallback).

    Returns plain text suitable for LLM consumption.
    Raises on network errors or empty content.
    """
    resp = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    html = resp.text

    text = _extract_with_bs4(html)
    if not text:
        text = _extract_with_regex(html)

    if len(text) < 100:
        raise ValueError(f"Page at {url} returned too little text ({len(text)} chars)")

    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "\n\n[... truncated ...]"

    return text


def _extract_with_bs4(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        logger.debug("[html_fallback] beautifulsoup4 not installed, using regex")
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()

    return soup.get_text(separator="\n", strip=True)


def _extract_with_regex(html: str) -> str:
    """Minimal HTML-to-text when BS4 is not available."""
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"</?(p|div|h[1-6]|li|tr|td|th)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
