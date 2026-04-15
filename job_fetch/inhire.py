"""Fetch a single inhire.io job offer by URL -> plain text.

URL format:
  https://app.inhire.io/oferty-pracy/{category}/{slug},oferta,{UUID}
  https://app.inhire.io/job-offers/{category}/{slug},oferta,{UUID}

Strategy:
  1. Try cloudscraper — individual offer pages may be SSR or have JSON-LD.
  2. Try JSON-LD (application/ld+json) structured data.
  3. Try BeautifulSoup DOM extraction.
  4. Try Playwright headless render if the above return too little text.
  5. Last resort: html_fallback.fetch_html().
"""

import json
import logging
import re

import cloudscraper

logger = logging.getLogger(__name__)
TIMEOUT = 25
_scraper = cloudscraper.create_scraper()


def fetch_inhire(url: str) -> str:
    """Fetch inhire.io offer and return plain text for LLM consumption."""
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"[inhire] HTTP fetch failed ({e}), trying html_fallback")
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    # Try structured JSON-LD first
    text = _try_json_ld(html)
    if text and len(text) > 150:
        return text

    # Try BeautifulSoup DOM
    text = _try_bs4(html)
    if text and len(text) > 150:
        return text

    # Try Playwright if the page is empty (full SPA, no SSR)
    text = _try_playwright(url)
    if text and len(text) > 150:
        return text

    logger.warning("[inhire] All strategies returned too little text, using html_fallback")
    from job_fetch.html_fallback import fetch_html
    return fetch_html(url)


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
                return _format_job_posting(item)
    return ""


def _format_job_posting(jp: dict) -> str:
    parts = []
    parts.append(f"Job Title: {jp.get('title', 'N/A')}")

    org = jp.get("hiringOrganization") or {}
    if isinstance(org, dict):
        parts.append(f"Company: {org.get('name', 'N/A')}")

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
            for lc in loc
            if isinstance(lc, dict)
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


def _try_bs4(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    parts = []

    h1 = soup.find("h1")
    if h1:
        parts.append(f"Job Title: {h1.get_text(strip=True)}")
    else:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            parts.append(f"Job Title: {og_title['content']}")

    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        parts.append(f"Summary: {og_desc['content']}")

    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()

    main = soup.find("main") or soup.find("article") or soup.find("div", {"role": "main"})
    content = main or soup.body
    if content:
        text = content.get_text(separator="\n", strip=True)
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if lines:
            parts.append("\n--- Page Content ---\n" + "\n".join(lines[:200]))

    result = "\n".join(parts)
    return result if len(result) > 100 else ""


def _try_playwright(url: str) -> str:
    """Use headless Chromium to render the SPA and extract job text."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("[inhire] playwright not installed, skipping headless fetch")
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
                await page.goto(url, wait_until="networkidle", timeout=30_000)
                # Try to get job data from Vuex store
                offer = await page.evaluate("""() => {
                    try {
                        const appEl = document.getElementById('app');
                        // Vue 3
                        const va = appEl.__vue_app__;
                        if (va) {
                            const store = va.config.globalProperties.$store;
                            if (store && store.state && store.state.offers) {
                                return store.state.offers.offer || store.state.offers.currentOffer || null;
                            }
                        }
                        // Vue 2
                        const v2 = appEl.__vue__;
                        if (v2 && v2.$store && v2.$store.state && v2.$store.state.offers) {
                            return v2.$store.state.offers.offer || v2.$store.state.offers.currentOffer || null;
                        }
                    } catch(e) {}
                    return null;
                }""")

                if offer and isinstance(offer, dict):
                    return _format_offer_dict(offer)

                # Fallback: get inner text of page
                text = await page.inner_text("body")
                lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
                return "\n".join(lines[:300]) if lines else ""
            finally:
                await browser.close()

    try:
        return asyncio.run(_run())
    except Exception as e:
        logger.warning(f"[inhire] Playwright job fetch failed: {e}")
        return ""


def _format_offer_dict(offer: dict) -> str:
    parts = []
    title = offer.get("name") or offer.get("title") or offer.get("jobTitle") or ""
    if title:
        parts.append(f"Job Title: {title}")
    company = offer.get("company") or offer.get("companyName") or offer.get("employer") or ""
    if isinstance(company, dict):
        company = company.get("name", "")
    if company:
        parts.append(f"Company: {company}")
    location = offer.get("location") or offer.get("city") or ""
    if isinstance(location, dict):
        location = location.get("name") or location.get("city") or ""
    if location:
        parts.append(f"Location: {location}")
    salary = offer.get("salary") or ""
    if isinstance(salary, dict):
        lo = salary.get("from") or salary.get("min") or ""
        hi = salary.get("to") or salary.get("max") or ""
        currency = salary.get("currency", "PLN")
        salary = f"{lo}-{hi} {currency}" if (lo or hi) else ""
    if salary:
        parts.append(f"Salary: {salary}")
    desc = offer.get("description") or offer.get("requirements") or ""
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html(str(desc))}")
    return "\n".join(parts)


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
