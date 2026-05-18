"""
job_fetch — Fetch job posting text from a URL.

Usage:
    from job_fetch import fetch_job_text
    text = fetch_job_text("https://justjoin.it/job-offer/some-slug")
"""

import logging
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

from job_fetch.justjoin import fetch_justjoin
from job_fetch.nofluffjobs import fetch_nofluffjobs
from job_fetch.html_fallback import fetch_html

logger = logging.getLogger(__name__)

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "utm_id",
    "fbclid", "gclid", "campaignid", "adgroupid",
    "ref", "refId", "trackingId", "trk",
    "sendid", "send_date", "sug",
    "originToLandingJobPostings", "origin",
}


def _clean_url(url: str) -> str:
    """Strip tracking/UTM params before fetching — prevents Cloudflare false positives."""
    p = urlparse(url)
    qs = parse_qs(p.query, keep_blank_values=False)
    clean = {k: v for k, v in qs.items() if k not in _TRACKING_PARAMS}
    return urlunparse(p._replace(query=urlencode(clean, doseq=True)))


def _matches_domain(domain: str, host: str) -> bool:
    """Return True if domain is exactly host or a subdomain of host."""
    return domain == host or domain.endswith("." + host)


def fetch_job_text(url: str) -> str:
    """Fetch and return plain-text job posting from the given URL.

    Dispatches to a source-specific fetcher when possible,
    falls back to generic HTML extraction.
    Raises on failure (caller decides how to handle).
    """
    url = _clean_url(url)
    domain = (urlparse(url).hostname or "").lower()

    if _matches_domain(domain, "justjoin.it"):
        logger.info(f"[job_fetch] JustJoin detected: {url}")
        return fetch_justjoin(url)

    if _matches_domain(domain, "nofluffjobs.com"):
        logger.info(f"[job_fetch] NoFluffJobs detected: {url}")
        return fetch_nofluffjobs(url)

    if _matches_domain(domain, "linkedin.com"):
        logger.info(f"[job_fetch] LinkedIn detected: {url}")
        from job_fetch.linkedin import fetch_linkedin
        return fetch_linkedin(url)

    if _matches_domain(domain, "bulldogjob.com"):
        logger.info(f"[job_fetch] Bulldogjob detected: {url}")
        from job_fetch.bulldogjob import fetch_bulldogjob
        return fetch_bulldogjob(url)

    if _matches_domain(domain, "pracuj.pl"):
        logger.info(f"[job_fetch] Pracuj.pl detected: {url}")
        from job_fetch.pracuj import fetch_pracuj
        return fetch_pracuj(url)

    if _matches_domain(domain, "theprotocol.it"):
        logger.info(f"[job_fetch] theprotocol.it detected: {url}")
        from job_fetch.theprotocol import fetch_theprotocol
        return fetch_theprotocol(url)

    if _matches_domain(domain, "solid.jobs"):
        logger.info(f"[job_fetch] Solid.Jobs detected: {url}")
        from job_fetch.solidjobs import fetch_solidjobs
        return fetch_solidjobs(url)

    if _matches_domain(domain, "inhire.io"):
        logger.info(f"[job_fetch] Inhire.io detected: {url}")
        from job_fetch.inhire import fetch_inhire
        return fetch_inhire(url)

    if _matches_domain(domain, "jobleads.com"):
        logger.info(f"[job_fetch] JobLeads detected: {url}")
        from job_fetch.jobleads import fetch_jobleads
        return fetch_jobleads(url)

    if _matches_domain(domain, "arbeitnow.com"):
        logger.info(f"[job_fetch] Arbeitnow detected: {url}")
        from job_fetch.arbeitnow import fetch_arbeitnow
        return fetch_arbeitnow(url)

    if _matches_domain(domain, "remotive.com"):
        logger.info(f"[job_fetch] Remotive detected: {url}")
        from job_fetch.remotive import fetch_remotive
        return fetch_remotive(url)

    if _matches_domain(domain, "remoteok.com"):
        logger.info(f"[job_fetch] Remote OK detected: {url}")
        from job_fetch.remoteok import fetch_remoteok
        return fetch_remoteok(url)

    if _matches_domain(domain, "4dayweek.io"):
        logger.info(f"[job_fetch] 4dayweek.io detected: {url}")
        from job_fetch.fourdayweek import fetch_fourdayweek
        return fetch_fourdayweek(url)

    if _matches_domain(domain, "weworkremotely.com"):
        logger.info(f"[job_fetch] We Work Remotely detected: {url}")
        from job_fetch.weworkremotely import fetch_weworkremotely
        return fetch_weworkremotely(url)

    if _matches_domain(domain, "remoteleaf.com"):
        logger.info(f"[job_fetch] RemoteLeaf detected: {url}")
        from job_fetch.remoteleaf import fetch_remoteleaf
        return fetch_remoteleaf(url)

    # ATS providers — exact subdomain matches are intentional
    if domain == "apply.workable.com":
        logger.info(f"[job_fetch] Workable ATS detected: {url}")
        from job_fetch.ats_workable import fetch_ats_workable
        return fetch_ats_workable(url)

    if _matches_domain(domain, "greenhouse.io"):
        logger.info(f"[job_fetch] Greenhouse ATS detected: {url}")
        from job_fetch.ats_greenhouse import fetch_ats_greenhouse
        return fetch_ats_greenhouse(url)

    if domain == "jobs.lever.co":
        logger.info(f"[job_fetch] Lever ATS detected: {url}")
        from job_fetch.ats_lever import fetch_ats_lever
        return fetch_ats_lever(url)

    if _matches_domain(domain, "recruitee.com"):
        logger.info(f"[job_fetch] Recruitee ATS detected: {url}")
        from job_fetch.ats_recruitee import fetch_ats_recruitee
        return fetch_ats_recruitee(url)

    if domain == "jobs.ashbyhq.com":
        logger.info(f"[job_fetch] Ashby ATS detected: {url}")
        from job_fetch.ats_ashby import fetch_ats_ashby
        return fetch_ats_ashby(url)

    logger.info(f"[job_fetch] Generic HTML fetch: {url}")
    return fetch_html(url)
