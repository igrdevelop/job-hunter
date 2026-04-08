"""Fetch a single NoFluffJobs posting by URL → plain text."""

import re
import requests

POSTING_API = "https://nofluffjobs.com/api/posting"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://nofluffjobs.com/",
}
TIMEOUT = 20


def _extract_slug(url: str) -> str:
    match = re.search(r"/job/([a-zA-Z0-9_-]+)", url)
    if not match:
        raise ValueError(f"Cannot extract NoFluffJobs slug from URL: {url}")
    return match.group(1)


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def fetch_nofluffjobs(url: str) -> str:
    """Try the posting detail API first, fall back to HTML page."""
    slug = _extract_slug(url)

    try:
        return _fetch_via_api(slug)
    except Exception:
        pass

    from job_fetch.html_fallback import fetch_html
    return fetch_html(url)


def _fetch_via_api(slug: str) -> str:
    resp = requests.get(f"{POSTING_API}/{slug}", headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    parts = []
    parts.append(f"Job Title: {data.get('title', 'N/A')}")
    parts.append(f"Company: {data.get('name', 'N/A')}")

    location = data.get("location", {})
    places = location.get("places", [])
    remote = data.get("fullyRemote", False)
    loc_str = "Remote" if remote else ", ".join(p.get("city", "") for p in places)
    parts.append(f"Location: {loc_str}")

    seniority = data.get("seniority", [])
    if seniority:
        parts.append(f"Seniority: {', '.join(seniority)}")

    # Must-haves & nice-to-haves
    musts = data.get("requirements", {}).get("musts", [])
    nices = data.get("requirements", {}).get("nices", [])
    if musts:
        parts.append(f"Must-have: {', '.join(m.get('value', '') for m in musts)}")
    if nices:
        parts.append(f"Nice-to-have: {', '.join(n.get('value', '') for n in nices)}")

    # Salary
    salary = data.get("essentials", {}).get("salary", {})
    if salary:
        low = salary.get("from")
        high = salary.get("to")
        cur = salary.get("currency", "PLN")
        emp = salary.get("type", "")
        if low or high:
            parts.append(f"Salary: {low or '?'}–{high or '?'} {cur} {emp}")

    # Sections (description, requirements text, etc.)
    sections = data.get("sections", {})
    for key in ("requirements", "responsibilities", "description", "methodology", "environment"):
        content = sections.get(key, "")
        if content:
            parts.append(f"\n--- {key.title()} ---\n{_strip_html(content)}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError(f"NoFluffJobs posting {slug} returned almost no content")
    return text
