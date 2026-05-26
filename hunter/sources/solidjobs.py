"""
solid.jobs source — Polish developer-focused IT job board.

Strategy:
  The site is an Angular SPA — HTML scraping returns an empty shell.
  Instead, we parse the public RSS feed (https://solid.jobs/rss/job-offers)
  which contains all active listings with title, company, location, salary.

  We filter locally by title keywords and location since the RSS
  doesn't support server-side filtering.
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

import requests

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

RSS_URL = "https://solid.jobs/rss/job-offers"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}
DETAIL_HEADERS = {
    "User-Agent": HEADERS["User-Agent"],
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://solid.jobs/",
}
TIMEOUT = 30
DETAIL_TIMEOUT = 25


def normalize_solidjobs_offer_url(url: str) -> str:
    """Normalize RSS-style offer links to canonical Solid.Jobs offer paths."""
    u = (url or "").strip()
    if not u:
        return u
    u = u.split("?", 1)[0]
    return re.sub(r"(https://solid\.jobs/o/[^/]+)/rss/?$", r"\1", u, flags=re.I)


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


def _format_job_posting_ld(jp: dict) -> str:
    parts: list[str] = []
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
        cities: list[str] = []
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
        parts.append(
            f"\n--- Requirements ---\n"
            + (_strip_html(quals) if isinstance(quals, str) else str(quals))
        )

    text = "\n".join(parts)
    if len(text) < 50:
        return ""
    return text


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
                return _format_job_posting_ld(item)
    return ""


def _try_bs4(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []

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

    for tag in soup.find_all(
        ["script", "style", "nav", "footer", "header", "noscript", "svg"]
    ):
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


class SolidJobsSource(BaseSource):
    name = "solidjobs"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "solid.jobs" in host

    def fetch_text(self, url: str) -> str:
        """Fetch a Solid.Jobs offer; try JSON-LD → BS4 → generic HTML fallback."""
        from hunter.sources.html_fallback import fetch_html

        url = normalize_solidjobs_offer_url(url)
        if "/offer-not-found/" in url:
            logger.info("[solidjobs] offer-not-found URL — returning expired marker")
            return "Offer expired"
        try:
            resp = requests.get(url, headers=DETAIL_HEADERS, timeout=DETAIL_TIMEOUT)
            resp.raise_for_status()
            if "/offer-not-found/" in resp.url:
                logger.info("[solidjobs] redirected to offer-not-found — expired: %s", url)
                return "Offer expired"
            html = resp.text
        except Exception as e:
            logger.warning(f"[solidjobs] HTTP fetch failed ({e}), trying html_fallback")
            return fetch_html(url)

        text = _try_json_ld(html)
        if text and len(text) > 100:
            return text

        text = _try_bs4(html)
        if text and len(text) > 100:
            return text

        logger.warning("[solidjobs] All strategies failed, using html_fallback")
        return fetch_html(url)

    def search(self) -> list[Job]:
        raw_items = self._fetch_rss()
        logger.info(f"[solidjobs] RSS returned {len(raw_items)} total items")

        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for raw in raw_items:
            job = self._parse(raw)
            if not job or job.url in seen_urls:
                continue
            if not self._is_relevant(raw, job):
                continue
            seen_urls.add(job.url)
            jobs.append(job)

        logger.info(f"[solidjobs] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- RSS fetch -------------------------------------------------------------

    def _fetch_rss(self) -> list[dict]:
        try:
            resp = requests.get(RSS_URL, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[solidjobs] RSS fetch failed: {e}")
            return []

        return self._parse_rss_xml(resp.text)

    @staticmethod
    def _parse_rss_xml(xml_text: str) -> list[dict]:
        try:
            root = ElementTree.fromstring(xml_text)
        except ElementTree.ParseError as e:
            logger.error(f"[solidjobs] RSS parse error: {e}")
            return []

        results = []
        for item in root.iter("item"):
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")

            title = title_el.text.strip() if title_el is not None and title_el.text else ""
            link = link_el.text.strip() if link_el is not None and link_el.text else ""
            link = normalize_solidjobs_offer_url(link)
            desc = desc_el.text.strip() if desc_el is not None and desc_el.text else ""

            categories = []
            for cat_el in item.findall("category"):
                if cat_el.text:
                    categories.append(cat_el.text.strip())

            if not title or not link:
                continue

            # Description format: "Company • Location\nSalary"
            company = ""
            location = ""
            salary = ""

            desc_lines = desc.split("\n")
            if desc_lines:
                first_line = desc_lines[0].strip()
                if " \u2022 " in first_line:
                    parts = first_line.split(" \u2022 ", 1)
                    company = parts[0].strip()
                    location = parts[1].strip() if len(parts) > 1 else ""
                elif "\u2022" in first_line:
                    parts = first_line.split("\u2022", 1)
                    company = parts[0].strip()
                    location = parts[1].strip() if len(parts) > 1 else ""
                else:
                    company = first_line

            if len(desc_lines) > 1:
                salary = desc_lines[1].strip()

            # Detect work mode from title or location
            title_lower = title.lower()
            loc_lower = location.lower()
            work_mode = ""
            if "(remote)" in title_lower or "remote" in loc_lower:
                work_mode = "remote"
            elif "hybrid" in loc_lower or "hybrydowa" in loc_lower:
                work_mode = "hybrid"

            # Clean location — remove leading dash/comma
            location = re.sub(r"^[-,]\s*", "", location).strip()

            if work_mode == "remote" and location:
                location = f"{location} (Remote)"
            elif work_mode == "remote":
                location = "Remote"
            elif work_mode == "hybrid" and location:
                location = f"{location} (Hybrid)"

            results.append({
                "title": title,
                "company": company,
                "location": location or "Unknown",
                "salary": salary,
                "work_mode": work_mode,
                "categories": categories,
                "url": link,
                "_text": f"{title} {company} {location} {' '.join(categories)}",
            })

        return results

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()

        exclude_patterns = FILTER.get("exclude_patterns", [])
        for pat in exclude_patterns:
            if re.search(pat, title, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        blob = raw.get("_text", "")
        if isinstance(blob, str):
            text = blob.lower()
        elif isinstance(blob, (list, tuple)):
            text = " ".join(str(x) for x in blob).lower()
        else:
            text = str(blob).lower()
        combined = title + " " + text
        return any(kw in combined for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        if not title:
            return None

        company = (raw.get("company") or "Unknown").strip()
        location = (raw.get("location") or "Unknown").strip()
        salary = raw.get("salary") or None
        url = normalize_solidjobs_offer_url(raw.get("url", ""))

        if not url:
            return None

        categories = raw.get("categories", [])
        raw_data = dict(raw)
        if categories:
            raw_data["technology"] = [{"name": c} for c in categories]

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw_data,
        )
