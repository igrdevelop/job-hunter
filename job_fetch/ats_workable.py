"""Workable job posting → plain text.

Public job URLs like https://apply.workable.com/j/{shortcode} are a thin SPA shell;
visible text in HTML is ~empty. The full posting is available as JSON:
  GET https://apply.workable.com/api/v1/accounts/{account_slug}/jobs/{shortcode}

If the account slug is not in the path, it is read from a ``link rel=canonical``-style
``href=.../slug/j/shortcode`` in the shell HTML, or we try `workable` slugs from
`hunter/ats_companies.json`.
"""

from __future__ import annotations

import json
import logging
import re
from urllib.parse import unquote, urlparse

import requests

from job_fetch.html_fallback import HEADERS as HTML_HEADERS
from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)

# Browser-like JSON request (align with hunter.ats.workable)
API_HEADERS = {
    **HTML_HEADERS,
    "Accept": "application/json",
    "Referer": "https://apply.workable.com/",
}
TIMEOUT = 30
ACCOUNT_RE = re.compile(
    r"https://apply\.workable\.com/([a-z0-9][-a-z0-9]*)/j/[A-Fa-f0-9]+",
    re.IGNORECASE,
)


def fetch_ats_workable(url: str) -> str:
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
            f"[ats:workable] could not resolve account slug for shortcode {shortcode}, using HTML (likely empty)"
        )
        return fetch_html(url)

    text = _fetch_workable_json_job(slug, shortcode, referer=url)
    if text and len(text) >= 100:
        return text

    return fetch_html(url)


def _parse_workable_path(path: str, full_url: str) -> tuple[str | None, str | None]:
    path = unquote(path or "")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return None, None
    if len(parts) == 2 and parts[0].lower() == "j":
        return None, parts[1]
    if len(parts) == 3 and parts[1].lower() == "j":
        return parts[0], parts[2]
    return None, None


def _discover_workable_account_slug(shortcode: str) -> str | None:
    """GET /j/{shortcode} shell and find account slug, or try ats_companies.json."""
    shell = f"https://apply.workable.com/j/{shortcode}"
    try:
        resp = requests.get(shell, headers=HTML_HEADERS, timeout=TIMEOUT)
        resp.raise_for_status()
        m = ACCOUNT_RE.search(resp.text)
        if m:
            return m.group(1)
    except Exception as e:
        logger.debug(f"[ats:workable] shell fetch for slug discovery: {e}")

    for candidate in _workable_slugs_from_config():
        if _job_json_exists(candidate, shortcode):
            return candidate
    return None


def _workable_slugs_from_config() -> list[str]:
    try:
        from hunter.config import ATS_COMPANIES_PATH
    except Exception:
        return []
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


def _job_json_exists(account_slug: str, shortcode: str) -> bool:
    u = f"https://apply.workable.com/api/v1/accounts/{account_slug}/jobs/{shortcode}"
    try:
        r = requests.get(
            u,
            headers={**API_HEADERS, "Referer": f"https://apply.workable.com/{account_slug}/j/{shortcode}"},
            timeout=12,
        )
        return r.status_code == 200 and "json" in r.headers.get("content-type", "")
    except Exception:
        return False


def _fetch_workable_json_job(account_slug: str, shortcode: str, referer: str) -> str:
    u = f"https://apply.workable.com/api/v1/accounts/{account_slug}/jobs/{shortcode}"
    try:
        resp = requests.get(
            u,
            headers={**API_HEADERS, "Referer": referer or f"https://apply.workable.com/{account_slug}/j/{shortcode}"},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning(f"[ats:workable] JSON job fetch failed: {e}")
        return ""

    if not isinstance(data, dict):
        return ""
    return _workable_dict_to_text(data)


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
        body = _html_to_text(str(raw))
        if body:
            lines.append("")
            lines.append(f"{label}:")
            lines.append(body)

    return "\n".join(lines).strip()


def _html_to_text(html: str) -> str:
    html = html.strip()
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
    except Exception:
        t = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", t).strip()


def _workable_text_for_tests(data: dict) -> str:
    """Exposed for unit tests of formatting only."""
    return _workable_dict_to_text(data)
