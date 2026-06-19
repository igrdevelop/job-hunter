"""
Email parsers for job aggregators.

Each parser receives (subject, body_text, body_html) from one email
and returns a list[Job] with URLs extracted from that email.

title/company/location/salary fields are stubs — gmail_enricher.enrich_jobs()
fills in real values by fetching each job URL before the filter pipeline runs.
Dedup still works on URL regardless of stub content.

To add a new aggregator:
  1. Find the real "From:" domain in the email header
  2. Add @register("that-domain.com") below
  3. Write a regex that matches job URLs from that site
"""

import re
from urllib.parse import urlparse

from hunter.models import Job

# domain → parser function
PARSERS: dict[str, callable] = {}


def register(domain: str):
    def decorator(fn):
        PARSERS[domain] = fn
        return fn
    return decorator


def _title_from_url(url: str) -> str:
    """Best-effort human title from a job URL slug.

    A digest email's subject describes one job, but its body often links to several
    (incl. "recommended for you"), so using the subject as the stub title can pair a
    title with the wrong URL. The URL slug always belongs to its own URL, so deriving
    the stub title from it keeps title and URL consistent. Returns "" when the slug is
    just an id (e.g. LinkedIn /jobs/view/12345), so the caller can fall back.
    """
    # pracuj: /praca/<slug>,oferta,<id>
    m = re.search(r"/praca/([^,/?]+),oferta,", url)
    if m:
        slug = m.group(1)
    else:
        path = urlparse(url).path.rstrip("/")
        seg = path.rsplit("/", 1)[-1] if path else ""
        # bulldogjob: <hexid>-<slug> → drop the leading id
        id_prefixed = re.match(r"^[0-9a-f]{6,}-(.+)$", seg)
        slug = id_prefixed.group(1) if id_prefixed else seg

    words = [w for w in re.split(r"[-_]", slug) if w and not w.isdigit()]
    return " ".join(w.capitalize() for w in words)


def _jobs_from_urls(urls: list[str], source: str, subject: str) -> list[Job]:
    return [
        Job(
            title=_title_from_url(url) or subject,
            company=f"[{source}]",
            location="",
            salary=None,
            url=url,
            source=f"gmail_{source}",
        )
        for url in dict.fromkeys(urls)  # deduplicate while preserving order
    ]


# ── LinkedIn ──────────────────────────────────────────────────────────────────
# Sends "10 new jobs for you" / "New jobs similar to ..." / "<query>: ..." digests.
# Hrefs in those emails are wrapped in LinkedIn's click-tracker, so the path is
# almost always /comm/jobs/view/<id> (with refId/trk query params), not the bare
# /jobs/view/<id> users see in their browser. We accept both, then canonicalize.

@register("linkedin.com")
def parse_linkedin(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    ids = re.findall(r'linkedin\.com/(?:comm/)?jobs/view/(\d{8,12})', html)
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
# Site canonical URLs: https://www.pracuj.pl/praca/some-job,oferta,XXXXXXXX
# Recommendation digests (rekomendacje@wysylka.pracuj.pl) send the bare-host
# variant https://pracuj.pl/praca/...,oferta,X?sendid=...&utm_*=... — the www.
# subdomain is dropped, and there's no separate click-tracker host. Accept both
# and canonicalize to the www. form so normalize_url's path-id treatment + dedup
# stay stable across email and scraped links.

@register("pracuj.pl")
def parse_pracuj(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    # Only use full URLs that contain the title slug — slug-less URLs (/praca/oferta,ID)
    # are invalid on pracuj.pl and will show "not found"
    direct = re.findall(
        r'https://(?:www\.)?pracuj\.pl/praca/[^">\s]+,oferta,\d+[^">\s]*', html
    )
    clean = []
    for url in direct:
        if not re.search(r'/praca/[^/]+,oferta,\d+', url):
            continue
        url = re.sub(r'\?.*$', '', url)
        url = re.sub(r'^https://pracuj\.pl/', 'https://www.pracuj.pl/', url)
        clean.append(url)
    return _jobs_from_urls(clean, "pracuj", subject)
