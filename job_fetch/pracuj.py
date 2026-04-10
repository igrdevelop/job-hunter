"""Fetch a single Pracuj.pl job offer by URL -> plain text.

URL format: https://www.pracuj.pl/praca/{slug},oferta,{id}

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


def _extract_offer_id(url: str) -> str:
    m = re.search(r",oferta,(\d+)", url)
    if not m:
        raise ValueError(f"Cannot extract Pracuj.pl offer ID from URL: {url}")
    return m.group(1)


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


def fetch_pracuj(url: str) -> str:
    """Fetch Pracuj.pl offer and return plain text for LLM consumption."""
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        html = resp.text
    except Exception as e:
        logger.warning(f"[pracuj] HTTP fetch failed ({e}), trying html_fallback")
        from job_fetch.html_fallback import fetch_html
        return fetch_html(url)

    # Strategy 1: JSON-LD
    text = _try_json_ld(html)
    if text and len(text) > 100:
        return text

    # Strategy 2: __NEXT_DATA__
    text = _try_next_data(html)
    if text and len(text) > 100:
        return text

    # Strategy 3: BeautifulSoup DOM parsing
    text = _try_bs4(html)
    if text and len(text) > 100:
        return text

    # Strategy 4: generic html_fallback
    logger.warning("[pracuj] All extraction strategies failed, using html_fallback")
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

        # JSON-LD can be a single object or a list
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

    # Location
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

    # Salary
    salary = jp.get("baseSalary") or {}
    if isinstance(salary, dict):
        value = salary.get("value") or {}
        if isinstance(value, dict):
            lo = value.get("minValue")
            hi = value.get("maxValue")
            currency = salary.get("currency", "PLN")
            if lo or hi:
                parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")

    # Employment type
    emp = jp.get("employmentType")
    if emp:
        if isinstance(emp, list):
            emp = ", ".join(emp)
        parts.append(f"Employment: {emp}")

    # Description (HTML)
    desc = jp.get("description", "")
    if desc:
        parts.append(f"\n--- Job Description ---\n{_strip_html(desc)}")

    # Skills / qualifications
    quals = jp.get("qualifications") or jp.get("skills") or ""
    if quals:
        parts.append(f"\n--- Requirements ---\n{_strip_html(quals) if isinstance(quals, str) else quals}")

    text = "\n".join(parts)
    if len(text) < 50:
        return ""
    return text


def _try_next_data(html: str) -> str:
    """Extract job data from __NEXT_DATA__ JSON embedded in the page."""
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        html, re.S,
    )
    if not m:
        return ""

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return ""

    page_props = data.get("props", {}).get("pageProps", {})
    offer = page_props.get("offer") or page_props.get("dehydratedState", {})

    if not isinstance(offer, dict) or not offer:
        return ""

    return _format_next_data_offer(offer)


def _format_next_data_offer(offer: dict) -> str:
    parts = []

    title = offer.get("jobTitle") or offer.get("title") or offer.get("name", "N/A")
    parts.append(f"Job Title: {title}")

    company = offer.get("companyName") or offer.get("employer", {}).get("name", "N/A")
    parts.append(f"Company: {company}")

    # Location
    locations = offer.get("locations") or offer.get("workplaces") or []
    if isinstance(locations, list):
        cities = []
        for loc in locations:
            if isinstance(loc, dict):
                city = loc.get("city") or loc.get("label", "")
                if city:
                    cities.append(city)
            elif isinstance(loc, str):
                cities.append(loc)
        if cities:
            parts.append(f"Location: {', '.join(cities)}")

    work_modes = offer.get("workModes") or offer.get("workSchedules") or []
    if work_modes:
        if isinstance(work_modes, list):
            parts.append(f"Work mode: {', '.join(str(w) for w in work_modes)}")

    # Salary
    salary = offer.get("salary") or offer.get("salaryDisplayText") or ""
    if isinstance(salary, dict):
        lo = salary.get("from") or salary.get("min")
        hi = salary.get("to") or salary.get("max")
        currency = salary.get("currency", "PLN")
        if lo or hi:
            parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")
    elif isinstance(salary, str) and salary:
        parts.append(f"Salary: {salary}")

    # Technologies / requirements
    techs = offer.get("technologies") or offer.get("expectedTechnologies") or []
    if techs:
        if isinstance(techs, list):
            tech_names = []
            for t in techs:
                if isinstance(t, dict):
                    tech_names.append(t.get("name", str(t)))
                else:
                    tech_names.append(str(t))
            parts.append(f"Technologies: {', '.join(tech_names)}")

    # Description sections
    for key in ("description", "responsibilities", "requirements", "offered", "benefits"):
        val = offer.get(key, "")
        if val and isinstance(val, str):
            parts.append(f"\n--- {key.title()} ---\n{_strip_html(val)}")
        elif val and isinstance(val, list):
            items = "\n".join(f"- {_strip_html(str(v))}" for v in val)
            parts.append(f"\n--- {key.title()} ---\n{items}")

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

    # Title from <h1> or og:title
    h1 = soup.find("h1")
    if h1:
        parts.append(f"Job Title: {h1.get_text(strip=True)}")
    else:
        og_title = soup.find("meta", property="og:title")
        if og_title and og_title.get("content"):
            parts.append(f"Job Title: {og_title['content']}")

    # Company name — often in a data attribute or specific element
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        parts.append(f"Summary: {og_desc['content']}")

    # Remove navigation/footer noise
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "noscript", "svg"]):
        tag.decompose()

    # Find main content area
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
