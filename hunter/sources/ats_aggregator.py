"""
ATS Aggregator — single Source that reads career pages of many companies
through their ATS provider's public JSON API.

Companies are listed in hunter/ats_companies.json. Each entry has:
  - slug: company id inside the ATS (e.g. "netguru" on Workable)
  - provider: workable | greenhouse | lever | recruitee | ashby (see hunter/ats/*.py)
  - name (optional): display name; defaults to slug.title()

Adding a new ATS provider: implement hunter/ats/<name>.py and register
it in PROVIDERS below. Adding a new company: append a JSON entry, no code.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from hunter.ats.ashby import AshbyProvider
from hunter.ats.base import ATSProvider
from hunter.ats.greenhouse import GreenhouseProvider
from hunter.ats.lever import LeverProvider
from hunter.ats.recruitee import RecruiteeProvider
from hunter.ats.workable import WorkableProvider
from hunter.config import ATS_COMPANIES_PATH
from hunter.models import Job
from hunter.sources.base import BaseSource

logger = logging.getLogger(__name__)

PROVIDERS: dict[str, ATSProvider] = {
    "workable": WorkableProvider(),
    "greenhouse": GreenhouseProvider(),
    "lever": LeverProvider(),
    "recruitee": RecruiteeProvider(),
    "ashby": AshbyProvider(),
}


class AtsAggregatorSource(BaseSource):
    name = "ats_aggregator"

    def matches_url(self, url: str) -> bool:
        """Match URLs hosted on any of the supported ATS providers.

        Mirror the substring/suffix checks from the legacy job_fetch dispatcher
        so detail-page routing stays behaviour-preserving.
        """
        host = (urlparse(url).hostname or "").lower()
        if "apply.workable.com" in host:
            return True
        if "greenhouse.io" in host:
            return True
        if "jobs.lever.co" in host:
            return True
        if host.endswith(".recruitee.com"):
            return True
        if "jobs.ashbyhq.com" in host:
            return True
        return False

    def fetch_text(self, url: str) -> str:
        """Workable goes through its public JSON API; others use html_fallback.

        Mirrors the legacy job_fetch dispatch — every ATS wrapper except
        Workable was a trivial fetch_html() call.
        """
        from hunter.sources.html_fallback import fetch_html

        host = (urlparse(url).hostname or "").lower()
        if "apply.workable.com" in host:
            return _fetch_workable_text(url)
        return fetch_html(url)

    def search(self) -> list[Job]:
        companies = load_companies(ATS_COMPANIES_PATH)
        if not companies:
            logger.info("[ats_aggregator] no companies configured")
            return []

        seen_urls: set[str] = set()
        jobs: list[Job] = []
        for company in companies:
            slug = (company.get("slug") or "").strip()
            provider_name = (company.get("provider") or "").strip().lower()
            display_name = (company.get("name") or "").strip() or None
            if not slug or not provider_name:
                logger.warning(f"[ats_aggregator] incomplete entry skipped: {company!r}")
                continue
            provider = PROVIDERS.get(provider_name)
            if provider is None:
                logger.warning(
                    f"[ats_aggregator] unknown provider '{provider_name}' for slug '{slug}' — skipped"
                )
                continue

            try:
                batch = provider.fetch(slug, display_name)
            except Exception as e:
                logger.warning(f"[ats_aggregator] {provider_name}:{slug} crashed: {e}")
                continue

            for job in batch:
                if not job.url or job.url in seen_urls:
                    continue
                if not self.matches_coarse_prefilter(job.title):
                    continue
                seen_urls.add(job.url)
                jobs.append(job)

        logger.info(f"[ats_aggregator] {len(jobs)} jobs after pre-filter")
        return jobs


_WORKABLE_TIMEOUT = 30
_WORKABLE_ACCOUNT_RE = re.compile(
    r"https://apply\.workable\.com/([a-z0-9][-a-z0-9]*)/j/[A-Fa-f0-9]+",
    re.IGNORECASE,
)


def _parse_workable_path(path: str, full_url: str) -> tuple[str | None, str | None]:
    """Return (account_slug, shortcode) from a workable URL path.

    Forms supported:
      /j/{shortcode}            → (None, shortcode)
      /{slug}/j/{shortcode}     → (slug, shortcode)
    """
    path = unquote(path or "")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None, None
    if len(parts) == 2 and parts[0].lower() == "j":
        return None, parts[1]
    if len(parts) == 3 and parts[1].lower() == "j":
        return parts[0], parts[2]
    return None, None


def _workable_html_headers() -> dict[str, str]:
    from hunter.sources.html_fallback import HEADERS
    return dict(HEADERS)


def _workable_api_headers(referer: str) -> dict[str, str]:
    return {
        **_workable_html_headers(),
        "Accept": "application/json",
        "Referer": referer,
    }


def _workable_slugs_from_config() -> list[str]:
    try:
        with open(ATS_COMPANIES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.debug(f"[ats:workable] could not read {ATS_COMPANIES_PATH}: {e}")
        return []
    if not isinstance(data, dict):
        return []
    out: list[str] = []
    for c in data.get("companies") or []:
        if not isinstance(c, dict):
            continue
        if c.get("provider", "").lower() == "workable" and c.get("slug"):
            s = c["slug"].strip()
            if s:
                out.append(s)
    return out


def _workable_job_json_exists(account_slug: str, shortcode: str) -> bool:
    u = f"https://apply.workable.com/api/v1/accounts/{account_slug}/jobs/{shortcode}"
    try:
        r = requests.get(
            u,
            headers=_workable_api_headers(
                f"https://apply.workable.com/{account_slug}/j/{shortcode}"
            ),
            timeout=12,
        )
        return r.status_code == 200 and "json" in r.headers.get("content-type", "")
    except Exception:
        return False


def _discover_workable_account_slug(shortcode: str) -> str | None:
    """GET /j/{shortcode} shell and find account slug; or try ats_companies.json."""
    shell = f"https://apply.workable.com/j/{shortcode}"
    try:
        resp = requests.get(shell, headers=_workable_html_headers(), timeout=_WORKABLE_TIMEOUT)
        resp.raise_for_status()
        m = _WORKABLE_ACCOUNT_RE.search(resp.text)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug(f"[ats:workable] shell fetch for slug discovery: {e}")

    for candidate in _workable_slugs_from_config():
        if _workable_job_json_exists(candidate, shortcode):
            return candidate
    return None


def _workable_html_to_text(html: str) -> str:
    html = html.strip()
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    except Exception:
        t = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", t).strip()


def _workable_dict_to_text(data: dict) -> str:
    lines: list[str] = []
    t = (data.get("title") or "").strip()
    if t:
        lines.append(f"Title: {t}")

    loc = data.get("location")
    if isinstance(loc, dict):
        loc_bits = [loc.get("city"), loc.get("country"), loc.get("name")]
        loc_s = ", ".join(x for x in loc_bits if x)
        if not loc_s:
            loc_s = str(loc)
    else:
        loc_s = (str(loc).strip() if loc else "")

    if data.get("remote"):
        loc_s = f"{loc_s} (Remote)" if loc_s else "Remote"
    if loc_s:
        lines.append(f"Location: {loc_s}")
    if (data.get("type") or "").strip():
        lines.append(f"Type: {data['type']}")
    if (data.get("workplace") or "").strip():
        lines.append(f"Workplace: {data['workplace']}")

    for key, label in (
        ("description", "Description"),
        ("requirements", "Requirements"),
        ("benefits", "Benefits"),
    ):
        raw = data.get(key)
        if not raw or not str(raw).strip():
            continue
        body = _workable_html_to_text(str(raw))
        if body:
            lines.append("")
            lines.append(f"{label}:")
            lines.append(body)

    return "\n".join(lines).strip()


def _fetch_workable_json_job(account_slug: str, shortcode: str, referer: str) -> str:
    u = f"https://apply.workable.com/api/v1/accounts/{account_slug}/jobs/{shortcode}"
    try:
        resp = requests.get(
            u,
            headers=_workable_api_headers(
                referer or f"https://apply.workable.com/{account_slug}/j/{shortcode}"
            ),
            timeout=_WORKABLE_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[ats:workable] JSON job fetch failed: {e}")
        return ""
    if not isinstance(data, dict):
        return ""
    return _workable_dict_to_text(data)


def _fetch_workable_text(url: str) -> str:
    """Public job URL → plaintext via the Workable JSON API (or html_fallback)."""
    from hunter.sources.html_fallback import fetch_html

    logger.info(f"[ats:workable] fetching {url}")
    parsed = urlparse(url)
    if "apply.workable.com" not in (parsed.netloc or "").lower():
        return fetch_html(url)

    slug, shortcode = _parse_workable_path(parsed.path, url)
    if not shortcode:
        return fetch_html(url)

    if not slug:
        slug = _discover_workable_account_slug(shortcode)
    if not slug:
        logger.warning(
            f"[ats:workable] could not resolve account slug for shortcode {shortcode}, "
            "using HTML fallback"
        )
        return fetch_html(url)

    text = _fetch_workable_json_job(slug, shortcode, referer=url)
    if text and len(text) >= 100:
        return text
    return fetch_html(url)


def load_companies(path: Path) -> list[dict[str, Any]]:
    """Load and validate the companies list. Returns [] on missing/broken file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.info(f"[ats_aggregator] config not found: {path}")
        return []
    except Exception as e:
        logger.warning(f"[ats_aggregator] failed to load config {path}: {e}")
        return []

    if not isinstance(data, dict):
        return []
    raw_list = data.get("companies")
    if not isinstance(raw_list, list):
        return []
    return [c for c in raw_list if isinstance(c, dict)]
