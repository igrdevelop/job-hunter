"""Fetch a single jobleads.com job offer by URL -> plain text.

URL format:
  https://www.jobleads.com/pl/job/{title}--{city}--{hash}

Strategy:
  1. cloudscraper GET — works when Cloudflare challenge is soft.
     Detail pages often return 403, so we detect that and skip ahead.
  2. Try JSON-LD (application/ld+json, @type: JobPosting) — structured data.
  3. Try BeautifulSoup DOM — extract visible sections from detail page.
  4. Playwright headless browser — bypasses hard 403 blocks on detail pages.
  5. Last resort: html_fallback.fetch_html().
"""

import json
import logging
import re

import cloudscraper
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
TIMEOUT = 25

_scraper = cloudscraper.create_scraper()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.jobleads.com/",
}


def fetch_jobleads(url: str) -> str:
    """Fetch jobleads.com offer and return plain text for LLM consumption."""
    html = ""
    try:
        resp = _scraper.get(url, headers=HEADERS, timeout=TIMEOUT)
        if resp.status_code == 403:
            logger.info(f"[jobleads] 403 on detail page, trying Playwright: {url}")
        else:
            resp.raise_for_status()
            html = resp.text
    except Exception as e:
        logger.warning(f"[jobleads] HTTP fetch failed ({e}), trying Playwright")

    if html:
        # 1. Structured JSON-LD
        text = _try_json_ld(html)
        if text and len(text) > 150:
            return text

        # 2. BeautifulSoup DOM extraction
        text = _try_bs4(html)
        if text and len(text) > 150:
            return text

        logger.info("[jobleads] cloudscraper returned thin content, trying Playwright")

    # 3. Playwright headless — handles 403 and JS-rendered content
    text = _try_playwright(url)
    if text and len(text) > 150:
        return text

    logger.warning("[jobleads] all strategies returned too little text, using html_fallback")
    from job_fetch.html_fallback import fetch_html
    return fetch_html(url)


# ── JSON-LD ────────────────────────────────────────────────────────────────────

def _try_json_ld(html: str) -> str:
    matches = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.S,
    )
    for raw in matches:
        try:
            data = json.loads(raw.strip())
        except json.JSONDecodeError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if item.get("@type") == "JobPosting":
                return _format_json_ld(item)
    return ""


def _format_json_ld(jp: dict) -> str:
    parts = []
    parts.append(f"Job Title: {jp.get('title', 'N/A')}")

    org = jp.get("hiringOrganization") or {}
    if isinstance(org, dict) and org.get("name"):
        parts.append(f"Company: {org['name']}")

    loc = jp.get("jobLocation")
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        city = addr.get("addressLocality", "")
        country = addr.get("addressCountry", "")
        loc_str = ", ".join(filter(None, [city, country]))
        if loc_str:
            parts.append(f"Location: {loc_str}")
    elif isinstance(loc, list):
        cities = [
            (lc.get("address") or {}).get("addressLocality", "")
            for lc in loc if isinstance(lc, dict)
        ]
        if any(cities):
            parts.append(f"Location: {', '.join(c for c in cities if c)}")

    salary = jp.get("baseSalary") or {}
    if isinstance(salary, dict):
        value = salary.get("value") or {}
        if isinstance(value, dict):
            lo = value.get("minValue")
            hi = value.get("maxValue")
            currency = salary.get("currency", "PLN")
            if lo or hi:
                parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")

    desc = jp.get("description", "")
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html(desc)}")

    result = "\n".join(parts)
    return result if len(result) > 50 else ""


# ── BeautifulSoup DOM ──────────────────────────────────────────────────────────

def _try_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts = []

    # Title — h1 or og:title
    h1 = soup.find("h1")
    if h1:
        parts.append(f"Job Title: {h1.get_text(strip=True)}")
    else:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            parts.append(f"Job Title: {og['content']}")

    # Company — og:description often has "Company | Role" format on jobleads
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        parts.append(f"Summary: {og_desc['content']}")

    # Remove noise
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()

    # Job detail sections — jobleads uses data-testid attributes on detail pages
    detail_sections = [
        ("div", {"data-testid": "job-description"}),
        ("div", {"data-testid": "job-requirements"}),
        ("div", {"data-testid": "job-benefits"}),
        ("section", {}),
    ]
    extracted = False
    for tag, attrs in detail_sections:
        sections = soup.find_all(tag, attrs=attrs) if attrs else soup.find_all(tag)
        for sec in sections:
            text = sec.get_text(separator="\n", strip=True)
            if len(text) > 100:
                parts.append(f"\n--- {tag.title()} ---\n{text[:3000]}")
                extracted = True
        if extracted:
            break

    # Final fallback: body text
    if not extracted:
        main = soup.find("main") or soup.find("article") or soup.body
        if main:
            lines = [ln.strip() for ln in main.get_text(separator="\n", strip=True).splitlines() if ln.strip()]
            if lines:
                parts.append("\n--- Page Content ---\n" + "\n".join(lines[:300]))

    result = "\n".join(parts)
    return result if len(result) > 100 else ""


# ── Playwright fallback ────────────────────────────────────────────────────────

def _try_playwright(url: str) -> str:
    """Use headless Chromium to bypass 403 and render the detail page."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("[jobleads] playwright not installed, skipping headless fetch")
        return ""

    async def _run() -> str:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            page = await browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            try:
                await page.goto(url, wait_until="networkidle", timeout=40_000)
                html = await page.content()
            finally:
                await browser.close()
        return html

    try:
        html = asyncio.run(_run())
    except Exception as e:
        logger.warning(f"[jobleads] Playwright fetch failed: {e}")
        return ""

    if not html:
        return ""

    # Re-run the same extraction pipeline on Playwright-rendered HTML
    text = _try_json_ld(html)
    if text and len(text) > 150:
        return text
    return _try_bs4(html)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section|strong|em|span)[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
