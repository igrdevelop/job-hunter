"""
Pracuj.pl source (it.pracuj.pl sub-domain for IT jobs).

Strategy:
  1. Fetch listing pages for frontend/angular jobs in Wroclaw + remote.
     Pracuj.pl renders __NEXT_DATA__ JSON on SSR pages with job listing data.
  2. If __NEXT_DATA__ is unavailable, fall back to BeautifulSoup DOM parsing.
  3. Individual job text is fetched lazily by job_fetch/pracuj.py when LLM
     processing is triggered.

Listing URLs:
  https://it.pracuj.pl/praca/frontend;kw/wroclaw;wp?rd=30
  https://it.pracuj.pl/praca/angular;kw/wroclaw;wp?rd=30
  https://it.pracuj.pl/praca/frontend;kw?rd=0&remote=true
"""

import json
import logging
import re
from typing import Optional
from urllib.parse import urlparse

import cloudscraper

from hunter.config import FILTER
from hunter.models import Job
from hunter.sources.base import BaseSource
from hunter.tracker import normalize_url

logger = logging.getLogger(__name__)

BASE = "https://it.pracuj.pl"
OFFER_BASE = "https://www.pracuj.pl"

LISTING_URLS = [
    f"{BASE}/praca/frontend;kw/wroclaw;wp?rd=30",
    f"{BASE}/praca/angular;kw/wroclaw;wp?rd=30",
    f"{BASE}/praca/frontend;kw?rd=0&remote=true",
]

TIMEOUT = 25

_scraper = cloudscraper.create_scraper()


def _playwright_available() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


# ── Detail-page fetching helpers (ported from job_fetch/pracuj.py) ───────────

_ARCHIVED_PATTERNS = (
    'data-test="section-archived"',
    "data-apply-type=\"ArchivedApplyPanel\"",
    "Pracodawca zakończył zbieranie zgłoszeń",
    "oferta wygasła",
    "offer expired",
)


def _extract_archived_notice(html: str) -> str:
    """Return expiry notice text if the page HTML contains an archived marker."""
    html_lower = html.lower()
    for marker in _ARCHIVED_PATTERNS:
        if marker.lower() in html_lower:
            return "\nPracodawca zakończył zbieranie zgłoszeń na tę ofertę\n"
    return ""


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
    # JSON-LD on Pracuj sometimes omits description; without it the result is unusable.
    if not jp.get("description"):
        return ""

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


def _format_next_data_offer(offer: dict) -> str:
    parts: list[str] = []
    title = offer.get("jobTitle") or offer.get("title") or offer.get("name", "N/A")
    parts.append(f"Job Title: {title}")

    company = offer.get("companyName") or offer.get("employer", {}).get("name", "N/A")
    parts.append(f"Company: {company}")

    locations = offer.get("locations") or offer.get("workplaces") or []
    if isinstance(locations, list):
        cities: list[str] = []
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

    salary = offer.get("salary") or offer.get("salaryDisplayText") or ""
    if isinstance(salary, dict):
        lo = salary.get("from") or salary.get("min")
        hi = salary.get("to") or salary.get("max")
        currency = salary.get("currency", "PLN")
        if lo or hi:
            parts.append(f"Salary: {lo or '?'}-{hi or '?'} {currency}")
    elif isinstance(salary, str) and salary:
        parts.append(f"Salary: {salary}")

    techs = offer.get("technologies") or offer.get("expectedTechnologies") or []
    if techs and isinstance(techs, list):
        tech_names: list[str] = []
        for t in techs:
            if isinstance(t, dict):
                tech_names.append(t.get("name", str(t)))
            else:
                tech_names.append(str(t))
        parts.append(f"Technologies: {', '.join(tech_names)}")

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


def _try_next_data(html: str) -> str:
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

    if offer.get("isActive") is False:
        return "\nPracodawca zakończył zbieranie zgłoszeń na tę ofertę\n"

    return _format_next_data_offer(offer)


def _try_bs4_detail(html: str) -> str:
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


def _fetch_detail_html(url: str) -> str:
    """Try cloudscraper first, fall back to plain requests. Returns raw HTML or raises."""
    try:
        resp = _scraper.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text
    except Exception as cs_err:
        logger.warning(f"[pracuj] cloudscraper failed ({cs_err}), trying plain requests")
        import requests as _req
        from hunter.sources.html_fallback import HEADERS as _FB_HEADERS
        resp = _req.get(url, headers=_FB_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        return resp.text


class PracujSource(BaseSource):
    name = "pracuj"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "pracuj.pl" in host

    def fetch_text(self, url: str) -> str:
        """Fetch Pracuj.pl offer; cascade JSON-LD → __NEXT_DATA__ → BS4 → fallback.

        Appends an archived-notice line when the page HTML signals an expired offer.
        """
        from hunter.sources.html_fallback import fetch_html
        try:
            html = _fetch_detail_html(url)
        except Exception as e:
            logger.warning(f"[pracuj] all HTTP strategies failed ({e}), using html_fallback")
            return fetch_html(url)

        archived_notice = _extract_archived_notice(html)

        text = _try_json_ld(html)
        if text and len(text) > 100:
            return text + archived_notice

        text = _try_next_data(html)
        if text and len(text) > 100:
            return text + archived_notice

        text = _try_bs4_detail(html)
        if text and len(text) > 100:
            return text + archived_notice

        logger.warning("[pracuj] All extraction strategies failed, using html_fallback")
        result = fetch_html(url)
        return result + archived_notice if result else result

    def search(self) -> list[Job]:
        if _playwright_available():
            return self._search_playwright()
        logger.warning("[Pracuj] playwright not installed, falling back to cloudscraper")
        return self._search_cloudscraper()

    def _search_playwright(self) -> list[Job]:
        return self._run_search(use_playwright=True)

    def _search_cloudscraper(self) -> list[Job]:
        return self._run_search(use_playwright=False)

    def _run_search(self, use_playwright: bool) -> list[Job]:
        seen_norm_urls: set[str] = set()
        seen_group_ids: set[str] = set()
        jobs: list[Job] = []

        for url in LISTING_URLS:
            raw_jobs = (
                self._fetch_listing_playwright(url)
                if use_playwright
                else self._fetch_listing(url)
            )
            logger.info(f"[Pracuj] {url} -> {len(raw_jobs)} raw jobs")
            for raw in raw_jobs:
                job = self._parse(raw)
                if not job:
                    continue
                if not self._is_relevant(raw, job):
                    continue
                gid = raw.get("groupId")
                if gid is not None and str(gid).strip():
                    gs = str(gid).strip()
                    if gs in seen_group_ids:
                        continue
                    seen_group_ids.add(gs)
                nu = normalize_url(job.url)
                if nu in seen_norm_urls:
                    continue
                seen_norm_urls.add(nu)
                jobs.append(job)

        logger.info(f"[Pracuj] {len(jobs)} jobs after pre-filter")
        return jobs

    # -- Listing fetch ---------------------------------------------------------

    def _fetch_listing_playwright(self, url: str) -> list[dict]:
        from hunter.playwright_helper import chromium_page
        try:
            with chromium_page(url) as page:
                html = page.content()
        except Exception as e:
            logger.error(f"[Pracuj] Playwright fetch failed for {url}: {e}")
            return []

        jobs = self._extract_next_data(html)
        if jobs:
            return jobs
        jobs = self._extract_json_ld(html)
        if jobs:
            return jobs
        return self._extract_bs4(html)

    def _fetch_listing(self, url: str) -> list[dict]:
        try:
            resp = _scraper.get(url, timeout=TIMEOUT)
            resp.raise_for_status()
        except Exception as e:
            logger.error(f"[Pracuj] fetch listing {url}: {e}")
            return []

        # Try __NEXT_DATA__ first
        jobs = self._extract_next_data(resp.text)
        if jobs:
            return jobs

        # Fallback: JSON-LD listing
        jobs = self._extract_json_ld(resp.text)
        if jobs:
            return jobs

        # Fallback: BeautifulSoup DOM
        return self._extract_bs4(resp.text)

    def _extract_next_data(self, html: str) -> list[dict]:
        m = re.search(
            r'<script id="__NEXT_DATA__"[^>]*>(.+?)</script>',
            html, re.S,
        )
        if not m:
            return []

        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            return []

        page_props = data.get("props", {}).get("pageProps", {})

        # Legacy format: offers directly in pageProps
        for key in ("offers", "data", "groupedOffers", "results"):
            offers = page_props.get(key)
            if isinstance(offers, list) and offers:
                return offers
            if isinstance(offers, dict):
                for sub_key in ("offers", "items", "results"):
                    sub = offers.get(sub_key)
                    if isinstance(sub, list) and sub:
                        return sub

        # Current format (2025+): React Query dehydratedState
        offers = self._extract_dehydrated_state(page_props)
        if offers:
            return offers

        return self._find_offers_in_props(page_props)

    @staticmethod
    def _extract_dehydrated_state(page_props: dict) -> list[dict]:
        """Extract offers from React Query dehydratedState cache."""
        ds = page_props.get("dehydratedState", {})
        queries = ds.get("queries", [])

        all_offers = []
        for query in queries:
            state = query.get("state", {})
            qdata = state.get("data")
            if not isinstance(qdata, dict):
                continue
            for key in ("groupedOffers", "offers", "results", "items"):
                items = qdata.get(key)
                if isinstance(items, list) and items:
                    if isinstance(items[0], dict) and any(
                        k in items[0] for k in ("jobTitle", "title", "companyName", "offerUrl")
                    ):
                        all_offers.extend(items)
        return all_offers

    @staticmethod
    def _find_offers_in_props(props: dict) -> list[dict]:
        """Walk pageProps looking for a list of job-like dicts."""
        for val in props.values():
            if not isinstance(val, list) or len(val) < 2:
                continue
            sample = val[0]
            if isinstance(sample, dict) and any(
                k in sample for k in ("jobTitle", "title", "companyName", "offerUrl", "uri")
            ):
                return val
        return []

    def _extract_json_ld(self, html: str) -> list[dict]:
        matches = re.findall(
            r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html, re.S,
        )
        results = []
        for raw in matches:
            try:
                data = json.loads(raw.strip())
            except json.JSONDecodeError:
                continue

            items = data if isinstance(data, list) else [data]
            for item in items:
                if item.get("@type") == "ItemList":
                    for elem in item.get("itemListElement", []):
                        if isinstance(elem, dict):
                            results.append(elem)
                elif item.get("@type") == "JobPosting":
                    results.append(item)
        return results

    def _extract_bs4(self, html: str) -> list[dict]:
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return []

        soup = BeautifulSoup(html, "html.parser")
        jobs = []

        # Pracuj listing pages use <a> tags with data-test or specific classes for offers
        offer_links = soup.find_all("a", href=re.compile(r",oferta,\d+"))
        seen = set()
        for a_tag in offer_links:
            href = a_tag.get("href", "")
            if href in seen:
                continue
            seen.add(href)

            title_el = a_tag.find(["h2", "h3", "span", "div"], string=True)
            title = title_el.get_text(strip=True) if title_el else a_tag.get_text(strip=True)

            if not title or len(title) < 3:
                continue

            jobs.append({
                "jobTitle": title,
                "offerUrl": href,
                "_source": "bs4",
            })

        return jobs

    # -- Pre-filter ------------------------------------------------------------

    def _is_relevant(self, raw: dict, job: Job) -> bool:
        title = job.title.lower()

        exclude_levels = [lv.lower() for lv in FILTER.get("exclude_levels", [])]
        level = (
            raw.get("experienceLevel") or raw.get("positionLevels") or ""
        )
        if isinstance(level, list):
            level = " ".join(str(l).lower() for l in level)
        else:
            level = str(level).lower()
        if any(lv in level for lv in exclude_levels):
            return False

        exclude_patterns = FILTER.get("exclude_patterns", [])
        for pat in exclude_patterns:
            if re.search(pat, title, re.I):
                return False

        keywords = [kw.lower() for kw in FILTER.get("title_keywords", [])]
        return any(kw in title for kw in keywords)

    # -- Parser ----------------------------------------------------------------

    def _parse(self, raw: dict) -> Optional[Job]:
        # Handle both __NEXT_DATA__ format and JSON-LD format
        title = (
            raw.get("jobTitle")
            or raw.get("title")
            or raw.get("name")
            or ""
        ).strip()

        company = (
            raw.get("companyName")
            or raw.get("employer")
            or raw.get("company")
            or ""
        )
        if isinstance(company, dict):
            company = company.get("name", "")
        company = str(company).strip()

        if not title:
            return None

        url = self._build_url(raw)
        if not url:
            return None

        location = self._parse_location(raw)
        salary = self._parse_salary(raw)

        return Job(
            title=title,
            company=company or "Unknown",
            location=location,
            salary=salary,
            url=url,
            source=self.name,
            raw=raw,
        )

    @staticmethod
    def _build_url(raw: dict) -> str:
        title_slug = re.sub(r"[^\w-]", "-", (raw.get("jobTitle") or raw.get("title") or "").strip().lower())
        title_slug = re.sub(r"-{2,}", "-", title_slug).strip("-")

        def _fix(url: str) -> str:
            """Ensure URL has a slug before ,oferta,ID — pracuj.pl requires it."""
            if not url:
                return ""
            # Already full: https://...slug,oferta,ID
            if re.search(r"/praca/[^/]+,oferta,\d+", url):
                return url
            # Slug-less: /praca/oferta,ID — prepend title slug if available
            m = re.search(r"oferta[,/](\d+)", url)
            if m and title_slug:
                return f"{OFFER_BASE}/praca/{title_slug},oferta,{m.group(1)}"
            if m:
                return ""  # no slug available — discard rather than store broken URL
            return url

        for key in ("offerAbsoluteUri", "offerUrl", "uri", "url"):
            val = raw.get(key, "")
            if val:
                if val.startswith("http"):
                    return _fix(val)
                if val.startswith("/"):
                    return _fix(f"{OFFER_BASE}{val}")
                return _fix(f"{OFFER_BASE}/praca/{val}")

        # dehydratedState format: URL is inside nested "offers" list
        nested = raw.get("offers") or []
        if isinstance(nested, list):
            for o in nested:
                if isinstance(o, dict):
                    uri = o.get("offerAbsoluteUri") or o.get("offerUrl") or ""
                    if uri:
                        full = uri if uri.startswith("http") else f"{OFFER_BASE}{uri}"
                        fixed = _fix(full)
                        if fixed:
                            return fixed

        # JSON-LD format
        if raw.get("@type") == "JobPosting" and raw.get("url"):
            return _fix(raw["url"])

        return ""

    @staticmethod
    def _parse_location(raw: dict) -> str:
        # __NEXT_DATA__ format
        places = raw.get("offers") or []
        if isinstance(places, list):
            cities = set()
            remote = False
            for place in places:
                if isinstance(place, dict):
                    city = (
                        place.get("displayWorkplace")
                        or place.get("city")
                        or place.get("label")
                        or ""
                    )
                    if city:
                        cities.add(city.strip())
                    if place.get("remoteWork") or place.get("remote"):
                        remote = True
            if cities:
                loc = ", ".join(sorted(cities))
                return f"{loc} (Remote)" if remote else loc
            if remote:
                return "Remote"

        # Simple location fields
        location = raw.get("location") or raw.get("displayWorkplace") or ""
        if isinstance(location, dict):
            location = location.get("label") or location.get("city") or ""
        location = str(location).strip()

        remote = raw.get("remoteWork") or raw.get("remote", False)
        if location and remote:
            return f"{location} (Remote)"
        if location:
            return location
        if remote:
            return "Remote"

        return "Unknown"

    @staticmethod
    def _parse_salary(raw: dict) -> Optional[str]:
        sal_text = raw.get("salaryDisplayText") or raw.get("salary") or ""
        if isinstance(sal_text, str) and sal_text.strip():
            return sal_text.strip()

        if isinstance(sal_text, dict):
            lo = sal_text.get("from") or sal_text.get("min")
            hi = sal_text.get("to") or sal_text.get("max")
            currency = sal_text.get("currency", "PLN")
            if lo or hi:
                return f"{lo or '?'}-{hi or '?'} {currency}"

        # Check nested offers for salary
        offers = raw.get("offers") or []
        if isinstance(offers, list):
            for o in offers:
                if not isinstance(o, dict):
                    continue
                sal = o.get("salaryDisplayText") or o.get("salary") or ""
                if isinstance(sal, str) and sal.strip():
                    return sal.strip()

        return None
