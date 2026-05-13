"""
Email parsers for job aggregators.

Each parser receives (subject, body_text, body_html) from one email
and returns a list[Job] with URLs extracted from that email.

title/company/location fields are placeholders — the main pipeline's
dedup works on URL, and filters run after. The scraper sources already
enrich data for justjoin/nofluffjobs/linkedin when those jobs come from
their APIs. Gmail-sourced jobs arrive with minimal metadata but are still
deduped correctly since the same URLs appear in both channels.

To add a new aggregator:
  1. Find the real "From:" domain in the email header
  2. Add @register("that-domain.com") below
  3. Write a regex that matches job URLs from that site
"""

import re
from hunter.models import Job

# domain → parser function
PARSERS: dict[str, callable] = {}


def register(domain: str):
    def decorator(fn):
        PARSERS[domain] = fn
        return fn
    return decorator


def _jobs_from_urls(urls: list[str], source: str, subject: str) -> list[Job]:
    return [
        Job(
            title=subject,
            company=f"[{source}]",
            location="",
            salary=None,
            url=url,
            source=f"gmail_{source}",
        )
        for url in dict.fromkeys(urls)  # deduplicate while preserving order
    ]


# ── LinkedIn ──────────────────────────────────────────────────────────────────
# Sends "10 new jobs for you" digests.
# URLs: https://www.linkedin.com/jobs/view/1234567890

@register("linkedin.com")
def parse_linkedin(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    ids = re.findall(r'linkedin\.com/jobs/view/(\d{8,12})', html)
    urls = [f"https://www.linkedin.com/jobs/view/{jid}" for jid in ids]
    return _jobs_from_urls(urls, "linkedin", subject)


# ── NoFluffJobs ───────────────────────────────────────────────────────────────
# URLs: https://nofluffjobs.com/pl/job/some-job-slug-city

@register("nofluffjobs.com")
def parse_nofluffjobs(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r'https://nofluffjobs\.com/pl/job/[\w-]+', html)
    return _jobs_from_urls(urls, "nofluffjobs", subject)


# ── JustJoin.it ───────────────────────────────────────────────────────────────
# URLs: https://justjoin.it/offers/some-offer-slug
#       https://justjoin.it/job-offer/some-offer-slug

@register("justjoin.it")
def parse_justjoin(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r'https://justjoin\.it/(?:offers|job-offer)/[\w-]+', html)
    return _jobs_from_urls(urls, "justjoin", subject)


# ── Bulldogjob ────────────────────────────────────────────────────────────────
# URLs: https://bulldogjob.pl/companies/jobs/XXXXXXXX-some-title

@register("bulldogjob.pl")
def parse_bulldogjob(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r'https://bulldogjob\.(?:pl|com)/companies/jobs/[\w-]+', html)
    return _jobs_from_urls(urls, "bulldogjob", subject)


# ── Pracuj.pl ─────────────────────────────────────────────────────────────────
# URLs: https://www.pracuj.pl/praca/some-job,oferta,XXXXXXXX

@register("pracuj.pl")
def parse_pracuj(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    # Only use full URLs that contain the title slug — slug-less URLs (/praca/oferta,ID)
    # are invalid on pracuj.pl and will show "not found"
    direct = re.findall(r'https://www\.pracuj\.pl/praca/[^">\s]+,oferta,\d+[^">\s]*', html)
    clean = [re.sub(r'\?.*$', '', url) for url in direct
             if re.search(r'/praca/[^/]+,oferta,\d+', url)]
    return _jobs_from_urls(clean, "pracuj", subject)
