"""Fetch a single Bulldogjob posting by URL → plain text.

Individual job page has all data in window.__NEXT_DATA__ → __APOLLO_STATE__.
The Job entry contains structured HTML fields: offer (description), requirements,
plus structured data: position, mainTechnology, technologyTags, locations, salary.
"""

import json
import re
import requests

BASE = "https://bulldogjob.com"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}
TIMEOUT = 20


def _strip_html(html: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", html)
    text = re.sub(r"<li[^>]*>", "- ", text)
    text = re.sub(r"</?(p|div|h[1-6]|ul|ol|section|strong|em)[^>]*>", " ", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _extract_job_id(url: str) -> str:
    """Extract job ID slug from URL like /companies/jobs/{id}."""
    m = re.search(r"/companies/jobs/([^/?#]+)", url)
    if not m:
        raise ValueError(f"Cannot extract Bulldogjob job ID from URL: {url}")
    return m.group(1)


def fetch_bulldogjob(url: str) -> str:
    job_id = _extract_job_id(url)
    job_url = f"{BASE}/companies/jobs/{job_id}"

    resp = requests.get(job_url, headers=HEADERS, timeout=TIMEOUT)
    resp.raise_for_status()

    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
        resp.text,
        re.S,
    )
    if not m:
        raise ValueError(f"No __NEXT_DATA__ on Bulldogjob page: {job_url}")

    data = json.loads(m.group(1))
    apollo = (
        data.get("props", {})
        .get("pageProps", {})
        .get("__APOLLO_STATE__", {})
    )

    # Find the Job entry by matching key prefix
    job_key = f"Job:{job_id}"
    job_data = apollo.get(job_key)
    if not job_data:
        # Try partial match
        for key, val in apollo.items():
            if key.startswith("Job:") and isinstance(val, dict):
                job_data = val
                break

    if not job_data:
        raise ValueError(f"No Job data in APOLLO_STATE for {job_id}")

    return _build_text(job_data, apollo)


def _build_text(job: dict, apollo: dict) -> str:
    parts = []

    title = (job.get("position") or "N/A").strip()
    parts.append(f"Job Title: {title}")

    # Company name via __ref
    company_ref = job.get("company", {})
    if isinstance(company_ref, dict):
        ref_key = company_ref.get("__ref", "")
        company_data = apollo.get(ref_key, {})
        company = (company_data.get("name") or "N/A").strip()
    else:
        company = "N/A"
    parts.append(f"Company: {company}")

    # Location
    locations = job.get("locations", [])
    loc_parts = []
    for loc in locations:
        if isinstance(loc, dict):
            loc_inner = loc.get("location", {})
            city = (loc_inner.get("cityEn") or loc_inner.get("cityPl") or "").strip()
            if city:
                loc_parts.append(city)
    is_remote = job.get("remote", False)
    work_modes = job.get("workModes") or []
    if is_remote or "full-remote" in work_modes:
        loc_parts.append("Remote")
    if loc_parts:
        parts.append(f"Location: {', '.join(loc_parts)}")

    # Experience level
    level = (job.get("experienceLevel") or "").strip()
    if level:
        parts.append(f"Experience level: {level}")

    # Technology tags + main tech
    main_tech = (job.get("mainTechnology") or "").strip()
    tags = job.get("technologyTags") or []
    if main_tech:
        parts.append(f"Main technology: {main_tech}")
    if tags:
        parts.append(f"Technologies: {', '.join(tags)}")

    # Salary
    for sal_key in ("b2bSalary", "employmentSalary"):
        sal = job.get(sal_key) or {}
        money = sal.get("money")
        currency = (sal.get("currency") or "PLN").upper()
        timeframe = sal.get("timeframe", "")
        if money:
            label = f"{sal_key.replace('Salary', '')} salary: {money} {currency}"
            if timeframe:
                label += f"/{timeframe}"
            parts.append(label)
            break

    # Offer / description (HTML → text)
    offer_html = (job.get("offer") or "").strip()
    if offer_html:
        parts.append(f"\n--- Offer / Description ---\n{_strip_html(offer_html)}")

    # Requirements (HTML → text)
    req_html = (job.get("requirements") or "").strip()
    if req_html:
        parts.append(f"\n--- Requirements ---\n{_strip_html(req_html)}")

    text = "\n".join(parts)
    if len(text) < 50:
        raise ValueError(f"Bulldogjob page returned almost no content for job")
    return text
