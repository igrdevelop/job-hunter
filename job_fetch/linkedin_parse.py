"""
linkedin_parse.py — Parse LinkedIn job alert / search URLs into individual job ids.

Handles the typical alert URL format:
  https://www.linkedin.com/jobs/search/?currentJobId=123&originToLandingJobPostings=123%2C456%2C789&...

Usage:
    from job_fetch.linkedin_parse import parse_linkedin_job_ids, job_view_url, is_linkedin_search

    ids = parse_linkedin_job_ids(alert_url)  # ["123", "456", "789"]
    urls = [job_view_url(i) for i in ids]
"""

from urllib.parse import urlparse, parse_qs


def is_linkedin_url(url: str) -> bool:
    """True for any linkedin.com URL."""
    return "linkedin.com" in (urlparse(url).hostname or "")


def is_linkedin_search(url: str) -> bool:
    """True if URL is a LinkedIn jobs search/alert page (not a single job view)."""
    parsed = urlparse(url)
    if "linkedin.com" not in (parsed.hostname or ""):
        return False
    return "/jobs/search" in parsed.path or "/jobs/search" in url


def is_linkedin_view(url: str) -> bool:
    """True if URL is already a single job view."""
    return "linkedin.com" in (urlparse(url).hostname or "") and "/jobs/view/" in url


def parse_linkedin_job_ids(url: str) -> list[str]:
    """Extract deduplicated job ids from a LinkedIn search / alert URL.

    Reads from:
    - currentJobId param (single id)
    - originToLandingJobPostings param (comma-separated, URL-encoded)
    - jobIds param (alternative field name used by some alert variants)

    Returns ids in the order they appear (currentJobId first if unique).
    Returns empty list if no ids found.
    """
    qs = parse_qs(urlparse(url).query, keep_blank_values=False)

    ids: list[str] = []
    seen: set[str] = set()

    def _add(raw: str) -> None:
        for part in raw.replace("%2C", ",").split(","):
            jid = part.strip()
            if jid and jid not in seen:
                seen.add(jid)
                ids.append(jid)

    # currentJobId — single highlighted/active job
    for val in qs.get("currentJobId", []):
        _add(val)

    # originToLandingJobPostings — full list from the alert notification
    for val in qs.get("originToLandingJobPostings", []):
        _add(val)

    # jobIds — alternative param name
    for val in qs.get("jobIds", []):
        _add(val)

    return ids


def job_view_url(job_id: str) -> str:
    """Canonical URL for a single LinkedIn job posting."""
    return f"https://www.linkedin.com/jobs/view/{job_id}/"
