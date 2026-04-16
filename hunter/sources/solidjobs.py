"""
solid.jobs source — Polish developer-focused IT job board.

Strategy:
  The site is an Angular SPA — HTML scraping returns an empty shell.
  Instead, we parse the public RSS feed (https://solid.jobs/rss/job-offers)
  which contains all active listings with title, company, location, salary.

  We filter locally by title keywords and location since the RSS
  doesn't support server-side filtering.
"""

import logging
import re
from typing import Optional
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
TIMEOUT = 30


def normalize_solidjobs_offer_url(url: str) -> str:
    """Normalize RSS-style offer links to canonical Solid.Jobs offer paths."""
    u = (url or "").strip()
    if not u:
        return u
    u = u.split("?", 1)[0]
    return re.sub(r"(https://solid\.jobs/o/[^/]+)/rss/?$", r"\1", u, flags=re.I)


class SolidJobsSource(BaseSource):
    name = "solidjobs"

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
