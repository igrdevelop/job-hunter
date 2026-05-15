"""Fetch a single JustJoin.it job offer by URL → plain text."""

import re
import requests

DETAIL_API = "https://justjoin.it/api/candidate-api/offers"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://justjoin.it/",
}
TIMEOUT = 20


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|tr|td|th|table|section)[^>]*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_slug(url: str) -> str:
    # JustJoin uses both /job-offer/{slug} (old) and /offers/{slug} (new) formats.
    match = re.search(r"/(?:job-offer|offers)/([a-z0-9-]+)", url)
    if not match:
        raise ValueError(f"Cannot extract JustJoin slug from URL: {url}")
    return match.group(1)


def fetch_justjoin(url: str) -> str:
    """Fetch JustJoin offer via their candidate API and return structured text."""
    slug = _extract_slug(url)
    resp = requests.get(f"{DETAIL_API}/{slug}", headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()

    parts = []
    parts.append(f"Job Title: {data.get('title', 'N/A')}")
    parts.append(f"Company: {data.get('companyName', 'N/A')}")

    if not data.get("isActive", True):
        parts.append("Offer expired")

    city = data.get("city", "")
    workplace = data.get("workplaceType", "")
    parts.append(f"Location: {city} ({workplace})" if city else f"Location: {workplace}")

    experience = data.get("experienceLevel", "")
    if experience:
        parts.append(f"Experience Level: {experience}")

    skills = data.get("skills", [])
    if skills:
        skill_names = [f"{s.get('name', '')} ({s.get('level', '')})" for s in skills]
        parts.append(f"Required Skills: {', '.join(skill_names)}")

    emp_types = data.get("employmentTypes", [])
    for et in emp_types:
        low, high = et.get("from"), et.get("to")
        currency = (et.get("currency") or "PLN").upper()
        emp_type = (et.get("type") or "").upper()
        if low or high:
            salary = f"{low or '?'}–{high or '?'} {currency} {emp_type}".strip()
            parts.append(f"Salary: {salary}")

    body = data.get("body", "")
    if body:
        parts.append(f"\n--- Job Description ---\n{_strip_html(body)}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError(f"JustJoin offer {slug} returned almost no content")
    return text
