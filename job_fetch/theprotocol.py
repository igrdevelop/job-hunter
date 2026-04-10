"""Fetch a single theprotocol.it job offer by URL -> plain text.

URL formats:
  https://theprotocol.it/szczegoly/praca/{slug},oferta,{UUID}
  https://theprotocol.it/praca/{slug},oferta,{UUID}

Strategy:
  1. Fetch page HTML via requests
  2. Try JSON-LD structured data (<script type="application/ld+json">)
  3. Fall back to BeautifulSoup DOM parsing
  4. Last resort: html_fallback.fetch_html()
"""

import json
import re
import logging

import cloudscraper

logger = logging.getLogger(__name__)

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()


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


def fetch_theprotocol(url: str) -> str:
    """Fetch theprotocol.it offer and return plain text for LLM consumption."""
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"[theprotocol] HTTP fetch failed ({e}), trying html_fallback")
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    text = _try_json_ld(html)
    if text and len(text) > 100:
        return text

    text = _try_bs4(html)
    if text and len(text) > 100:
        return text

    logger.warning("[theprotocol] All strategies failed, using html_fallback")
    from job_fetch.html_fallback import fetch_html
    return fetch_html(url)


def _try_json_ld(html: str) -> str:
    """Extract job data from JSON-LD (application/ld+json) script tags."""
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
                return _format_job_posting_ld(item)
    return ""


def _format_job_posting_ld(jp: dict) -> str:
    parts = []

    parts.append(f"Job Title: {jp.get('title', 'N/A')}")

    org = jp.get("hiringOrganization") or {}
    if isinstance(org, dict):
        parts.append(f"Company: {org.get('name', 'N/A')}")

    loc = jp.get("jobLocation")
    if isinstance(loc, dict):
        address = loc.get("address") or {}
        city = address.get("addressLocality", "")
        country = address.get("addressCountry", "")
        loc_str = ", ".join(filter(None, [city, country]))
        if loc_str:
            parts.append(f"Location: {loc_str}")
    elif isinstance(loc, list):
        cities = []
        for l in loc:
            addr = (l.get("address") or {})
            c = addr.get("addressLocality", "")
            if c:
                cities.append(c)
        if cities:
            parts.append(f"Location: {', '.join(cities)}")

    salary = jp.get("baseSalary") or {}
    if isinstance(salary, dict):
        value = salary.get("value") or {}
        if isinstance(value, dict):
            lo = value.get("minValue")
            hi = value.get("maxValue")
            currency = salary.get("currency", "PLN")
            if lo or hi:
                parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")

    emp = jp.get("employmentType")
    if emp:
        if isinstance(emp, list):
            emp = ", ".join(emp)
        parts.append(f"Employment: {emp}")

    desc = jp.get("description", "")
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html(desc)}")

    quals = jp.get("qualifications") or jp.get("skills") or ""
    if quals:
        parts.append(f"\n--- Requirements ---\n{_strip_html(quals) if isinstance(quals, str) else quals}")

    text = "\n".join(parts)
    if len(text) < 50:
        return ""
    return text


def _try_bs4(html: str) -> str:
    """Extract job content via BeautifulSoup DOM parsing."""
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
    if main:
        text = main.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)

    if text:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        parts.append("\n--- Page Content ---\n" + "\n".join(lines[:200]))

    result = "\n".join(parts)
    if len(result) < 100:
        return ""
    return result
