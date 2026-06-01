"""
jobleads.com source — international job aggregator with strong PL coverage.

Strategy (tested 2026-04):
  The listing pages are server-side rendered — job cards are present in the
  HTML response without JavaScript execution. No __NEXT_DATA__ or public API.
  We use cloudscraper (handles Cloudflare) + BeautifulSoup DOM parsing.

  Card DOM structure:
    div[data-testid="seo-search-list-job-card-{N}"]
      h2                                    → title
      a[data-testid="search-job-card-link"] → absolute job URL
      p[data-testid="search-job-card-company"] > span  → company
      div[data-testid="search-job-card-chips"]
        div[data-testid="job-card-chip-location"]  → city / country
        remaining chips → work type (Hybrid/Remote/On-site), salary, contract

Listing URLs:
  https://www.jobleads.com/pl/jobs?q=angular&location=wroclaw
  https://www.jobleads.com/pl/jobs?q=frontend&location=wroclaw
  https://www.jobleads.com/pl/jobs?q=angular&location=poland
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import cloudscraper
from bs4 import BeautifulSoup

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

BASE = "https://www.jobleads.com"

LISTING_URLS = [
    f"{BASE}/pl/jobs?q=angular&location=wroclaw",
    f"{BASE}/pl/jobs?q=frontend&location=wroclaw",
    f"{BASE}/pl/jobs?q=angular&location=poland",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": BASE + "/",
}

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()

# Salary-like text: currency symbol + digits OR digits + currency symbol
# Covers both "PLN 168,000 - 275,000" and "168 000 - 275 000 PLN"
_SALARY_RE = re.compile(
    r"(PLN|USD|EUR|GBP)\s*[\d\s,.-]+\d|\d[\d\s,.]*(PLN|USD|EUR|GBP)",
    re.I,
)

# Work-type chip labels (used to detect remote/hybrid for location building)
_REMOTE_LABELS = {"remote", "fully remote", "zdalnie"}
_HYBRID_LABELS = {"hybrid", "hybrydowo", "hybrydowa"}


# ── Detail-page fetch (ported from job_fetch/jobleads.py) ──────────────────

# Stub written by apply_agent when Cloudflare blocks the detail page;
# user pastes below this line.
JOBLEADS_PASTE_MARKER = "=== Paste the full job description below this line ==="
MIN_MANUAL_BODY_LEN = 200


class JobLeadsCloudflareError(RuntimeError):
    """Raised when jobleads.com is blocked by Cloudflare and no manual posting exists."""


def try_load_manual_job_posting(url: str) -> Optional[str]:
    """Use job_posting.txt from a MANUAL tracker row when the user has pasted the description."""
    try:
        from hunter.tracker import manual_jobleads_job_posting_path
    except ImportError:
        return None
    path = manual_jobleads_job_posting_path(url)
    if not path or not path.is_file():
        return None
    data = path.read_text(encoding="utf-8", errors="replace")
    if JOBLEADS_PASTE_MARKER in data:
        body = data.split(JOBLEADS_PASTE_MARKER, 1)[1].strip()
    else:
        body = re.sub(r"^URL:\s*\S+\s*", "", data, count=1, flags=re.MULTILINE).strip()
    if len(body) < MIN_MANUAL_BODY_LEN:
        return None
    return f"URL: {url}\n\n{body}"


def _strip_html_detail(html: str) -> str:
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


def _format_detail_json_ld(jp: dict) -> str:
    parts: list[str] = []
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
        parts.append(f"\n--- Job Description ---\n{_strip_html_detail(desc)}")
    result = "\n".join(parts)
    return result if len(result) > 50 else ""


def _try_detail_json_ld(html: str) -> str:
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
                return _format_detail_json_ld(item)
    return ""


def _try_detail_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    h1 = soup.find("h1")
    if h1:
        parts.append(f"Job Title: {h1.get_text(strip=True)}")
    else:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            parts.append(f"Job Title: {og['content']}")
    og_desc = soup.find("meta", property="og:description")
    if og_desc and og_desc.get("content"):
        parts.append(f"Summary: {og_desc['content']}")
    for tag in soup.find_all(
        ["script", "style", "nav", "footer", "header", "noscript", "svg"]
    ):
        tag.decompose()
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
    if not extracted:
        main = soup.find("main") or soup.find("article") or soup.body
        if main:
            lines = [
                ln.strip()
                for ln in main.get_text(separator="\n", strip=True).splitlines()
                if ln.strip()
            ]
            if lines:
                parts.append("\n--- Page Content ---\n" + "\n".join(lines[:300]))
    result = "\n".join(parts)
    return result if len(result) > 100 else ""


def _try_detail_playwright(url: str) -> str:
    """Use headless Chromium to bypass 403 and render the detail page."""
    try:
        import asyncio
        from playwright.async_api import async_playwright
    except ImportError:
        logger.debug("[jobleads] playwright not installed, skipping headless fetch")
        return ""

    async def _run() -> str:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            page = await browser.new_page(user_agent=HEADERS["User-Agent"])
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

    text = _try_detail_json_ld(html)
    if text and len(text) > 150:
        return text
    return _try_detail_bs4(html)


class JobLeadsSource(BaseSource):
    name = "jobleads"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "jobleads.com" in host

    def fetch_text(self, url: str) -> str:
        """cloudscraper → JSON-LD → BS4 → Playwright → html_fallback cascade.

        Raises ``JobLeadsCloudflareError`` when all strategies are blocked, so the
        caller (apply_agent) can fall through to the MANUAL paste flow.
        """
        from hunter.sources.html_fallback import fetch_html

        manual = try_load_manual_job_posting(url)
        if manual:
            logger.info("[jobleads] using pasted job_posting.txt (MANUAL tracker flow)")
            return manual

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
            text = _try_detail_json_ld(html)
            if text and len(text) > 150:
                return text

            text = _try_detail_bs4(html)
            if text and len(text) > 150:
                return text

            logger.info("[jobleads] cloudscraper returned thin content, trying Playwright")

        text = _try_detail_playwright(url)
        if text and len(text) > 150:
            return text

        logger.warning("[jobleads] all strategies returned too little text, trying html_fallback")
        last = fetch_html(url)
        if last and len(last) > 150 and "just a moment" not in last.lower():
            return last
        raise JobLeadsCloudflareError(
            f"jobleads.com: could not load job description ({len(last or '')} chars). "
            "Cloudflare often blocks automated fetches — use MANUAL flow (tracker + job_posting.txt)."
        )

    def search(self) -> list[Job]:
        seen_urls: set[str] = set()
        jobs: list[Job] = []

        for listing_url in LISTING_URLS:
            try:
                raw_jobs = self._fetch_listing(listing_url)
                logger.info(f"[jobleads] {listing_url} -> {len(raw_jobs)} raw")
                for raw in raw_jobs:
                    job = self._parse(raw)
                    if not job or job.url in seen_urls:
                        continue
                    if not self._is_relevant(raw, job):
                        continue
                    seen_urls.add(job.url)
                    jobs.append(job)
            except Exception as e:
                logger.warning(f"[jobleads] listing failed, skipping {listing_url}: {e}")

        logger.info(f"[jobleads] {len(jobs)} jobs after pre-filter")
        return jobs

    # ── Listing fetch ──────────────────────────────────────────────────────────

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, headers=HEADERS, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[jobleads] HTTP failed for {url}: {e}")
            return []

        return self._parse_cards(resp.text)

    @staticmethod
    def _parse_cards(html: str) -> list[dict]:
        """Extract job card dicts from listing page HTML."""
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.find_all(
            "div",
            attrs={"data-testid": re.compile(r"^seo-search-list-job-card-\d+$")},
        )
        results = []
        for card in cards:
            raw = JobLeadsSource._extract_card(card)
            if raw:
                results.append(raw)
        return results

    @staticmethod
    def _extract_card(card) -> Optional[dict]:
        """Pull structured fields from a single job card element."""
        # Title
        h2 = card.find("h2")
        title = h2.get_text(strip=True) if h2 else ""
        if not title:
            return None

        # Absolute URL
        link = card.find("a", attrs={"data-testid": "search-job-card-link"})
        url = link["href"].split("?")[0] if link and link.get("href") else ""
        if not url:
            return None

        # Company
        company_el = card.find("p", attrs={"data-testid": "search-job-card-company"})
        company = company_el.get_text(strip=True) if company_el else ""

        # Chips: location + work type + salary
        chips_el = card.find("div", attrs={"data-testid": "search-job-card-chips"})
        location = ""
        work_type = ""
        salary = ""
        if chips_el:
            loc_el = chips_el.find("div", attrs={"data-testid": "job-card-chip-location"})
            location = loc_el.get_text(strip=True) if loc_el else ""

            # Remaining chips: detect work type and salary by content
            for chip in chips_el.find_all("div", recursive=True):
                text = chip.get_text(strip=True)
                if not text or chip == loc_el:
                    continue
                if _SALARY_RE.search(text):
                    salary = salary or text
                elif text.lower() in _REMOTE_LABELS | _HYBRID_LABELS | {"on-site", "on site", "stacjonarnie"}:
                    work_type = work_type or text

        return {
            "title": title,
            "url": url,
            "company": company,
            "location": location,
            "work_type": work_type,
            "salary": salary,
            "_text": f"{title} {company} {location} {work_type}",
        }

    # ── Pre-filter ─────────────────────────────────────────────────────────────

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()
        for pat in FILTER.get("exclude_patterns", []):
            if re.search(pat, title, re.I):
                return False
        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        text = (raw.get("_text") or "").lower()
        return any(kw in title + " " + text for kw in keywords)

    # ── Parser ─────────────────────────────────────────────────────────────────

    def _parse(self, raw: dict) -> Optional[Job]:
        title = (raw.get("title") or "").strip()
        if not title:
            return None

        url = (raw.get("url") or "").strip()
        if not url or not url.startswith("http"):
            return None

        company = (raw.get("company") or "Unknown").strip() or "Unknown"
        location = self._build_location(raw)
        salary = (raw.get("salary") or None)

        return Job(
            title=title,
            company=company,
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _build_location(raw: dict) -> str:
        city = (raw.get("location") or "").strip()
        work_type = (raw.get("work_type") or "").strip().lower()

        if work_type in _REMOTE_LABELS:
            return f"{city} (Remote)" if city else "Remote"
        if work_type in _HYBRID_LABELS:
            return f"{city} (Hybrid)" if city else "Hybrid"
        if city:
            return city
        return "Unknown"
