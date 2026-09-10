"""Generic HTML job-page fetcher — default fallback for BaseSource.fetch_text.

Used both as the default implementation in BaseSource and as a last-resort
fallback when no source matches a URL.
"""

import logging
import re
from urllib.parse import parse_qs, urlencode, urljoin, urlparse, urlunparse

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
# SSRF guard (docs/improvement-2026-09/05-SECURITY_PLAN.md M5): redirects are
# followed manually, one host-revalidation per hop — see fetch_html().
MAX_REDIRECTS_FOLLOWED = 3

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_content",
    "utm_term",
    "utm_id",
    "fbclid",
    "gclid",
    "campaignid",
    "adgroupid",
    "ref",
    "refId",
    "trackingId",
    "trk",
    "sendid",
    "send_date",
    "sug",
    "originToLandingJobPostings",
    "origin",
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

    This is the one fetcher in the sources package that ever hits an
    arbitrary, non-hardcoded host (every other source builds its request
    against its own known API domain). docs/improvement-2026-09/
    05-SECURITY_PLAN.md finding #6/M5: a validated public URL can still
    REDIRECT onto a private address, so automatic redirect-following is
    disabled and each hop is followed manually, revalidating the `Location`
    with `hunter.url_policy.validate_public_url` before it is fetched. The
    initial `url` itself is deliberately NOT validated here — this function
    is called deep inside most scrapers' own `fetch_text()` with a host they
    already control (see the module docstring on `hunter.url_policy`); the
    apply pipeline's entry point (`fetch_job_text(..., use_session=True)`)
    validates the untrusted starting URL before it ever reaches here.
    """
    from hunter.url_policy import validate_public_url

    current_url = url
    resp: requests.Response
    for hop in range(MAX_REDIRECTS_FOLLOWED + 1):
        resp = requests.get(current_url, headers=HEADERS, timeout=TIMEOUT, allow_redirects=False)
        if resp.is_redirect and "Location" in resp.headers:
            if hop == MAX_REDIRECTS_FOLLOWED:
                raise ValueError(f"Too many redirects fetching {url} (stopped at {current_url})")
            next_url = validate_public_url(urljoin(current_url, resp.headers["Location"]))
            logger.debug("[html_fallback] redirect %s -> %s", current_url, next_url)
            current_url = next_url
            continue
        break

    resp.raise_for_status()
    html = resp.text

    text = extract_text(html)

    if len(text) < 100:
        raise ValueError(f"Page at {url} returned too little text ({len(text)} chars)")

    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN] + "\n\n[... truncated ...]"

    return text


def extract_text(html: str) -> str:
    """HTML -> visible text (BeautifulSoup, regex fallback).

    Split out of ``fetch_html`` so a source that fetches the HTML itself — to
    inspect markup ``get_text()`` throws away, e.g. LinkedIn's apply CTA — can
    still produce the exact same text without a second request.
    """
    text = _extract_with_bs4(html)
    if not text:
        text = _extract_with_regex(html)
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
