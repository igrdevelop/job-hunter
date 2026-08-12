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

import hashlib
import html as html_mod
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


def _stub_company(source: str, url: str, *, unique: bool) -> str:
    """Placeholder company for a job the email didn't name.

    ``unique=True`` appends a per-URL tag. The subject fallback below gives
    EVERY url of one email the same title, and with a shared ``[source]``
    company that makes `tracker.dedup_key(company, title)` identical for all
    of them — so the hunt's company+title dedup silently dropped every
    vacancy of a digest except the first ("Dup company+title" in the logs,
    25 such discards in one 19h window on the live bot). A per-URL tag keeps
    each stub its own vacancy until enrichment or a real card title lands.
    """
    base = f"[{source}]"
    if not unique:
        return base
    seg = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    tag = seg or hashlib.md5(url.encode(), usedforsecurity=False).hexdigest()[:8]
    return f"{base}#{tag}"


def _jobs_from_urls(
    urls: list[str],
    source: str,
    subject: str,
    cards: dict[str, tuple[str, str]] | None = None,
) -> list[Job]:
    """Build stub jobs for one email's URLs.

    ``cards`` maps a url to the (title, company) the email itself showed for
    it — see `_linkedin_cards`. Anything it doesn't cover falls back to the
    URL slug and finally to the email subject, which is only ever right for
    ONE of the linked jobs (see `_stub_company`).
    """
    cards = cards or {}
    jobs = []
    for url in dict.fromkeys(urls):  # deduplicate while preserving order
        card_title, card_company = cards.get(url, ("", ""))
        title = card_title or _title_from_url(url)
        from_subject = not title
        jobs.append(
            Job(
                title=title or subject,
                company=card_company or _stub_company(source, url, unique=from_subject),
                location="",
                salary=None,
                url=url,
                source=f"gmail_{source}",
            )
        )
    return jobs


# ── LinkedIn ──────────────────────────────────────────────────────────────────
# Sends "10 new jobs for you" / "New jobs similar to ..." / "<query>: ..." digests.
# Hrefs in those emails are wrapped in LinkedIn's click-tracker, so the path is
# almost always /comm/jobs/view/<id> (with refId/trk query params), not the bare
# /jobs/view/<id> users see in their browser. We accept both, then canonicalize.


# Each job in a LinkedIn alert is a card: an <a> to /jobs/view/<id> whose text
# is the job title, followed by a "<Company> · <City> (Remote|Hybrid)" line.
# The same id also appears in logo/tracking anchors with empty text — first
# non-empty text wins.
_LINKEDIN_CARD_RE = re.compile(r"<a\b[^>]*?jobs/view/(\d{8,12})[^>]*>(.*?)</a>", re.I | re.S)
# How far past </a> the "<Company> · <Location>" line sits (markup in between).
_LINKEDIN_COMPANY_WINDOW = 600
_TAG_RE = re.compile(r"<[^>]+>")
# Shortest string still plausible as a job title — keeps one-letter anchor
# labels ("a", "›") from being mistaken for one.
_MIN_CARD_TITLE_LEN = 5
_MAX_CARD_COMPANY_LEN = 80


def _visible_text(fragment: str) -> str:
    return re.sub(r"\s+", " ", html_mod.unescape(_TAG_RE.sub(" ", fragment))).strip()


def _linkedin_cards(body_html: str) -> dict[str, tuple[str, str]]:
    """Map each job URL in a LinkedIn alert to its own (title, company).

    A digest email links the job named in the subject PLUS several "similar"
    ones, so the subject is the right title for at most one of them. Reading
    each card's own title/company is what keeps the title-based filters (and
    the company+title dedup key) pointed at the right vacancy — a wrong
    title let a Java/Scala contract role and a "Software Engineer" reach
    generation on 2026-08-12.

    Missing or implausible values are simply left out; the caller falls back
    to the old subject-based stub for those.
    """
    normalized = re.sub(r"\s+", " ", body_html or "")
    cards: dict[str, tuple[str, str]] = {}
    for m in _LINKEDIN_CARD_RE.finditer(normalized):
        jid = m.group(1)
        if jid in cards:
            continue
        title = _visible_text(m.group(2))
        if len(title) < _MIN_CARD_TITLE_LEN:
            continue
        tail = _visible_text(normalized[m.end() : m.end() + _LINKEDIN_COMPANY_WINDOW])
        company = tail.split("·")[0].strip() if "·" in tail else ""
        if len(company) > _MAX_CARD_COMPANY_LEN:
            company = ""
        cards[jid] = (title, company)
    return cards


@register("linkedin.com")
def parse_linkedin(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    ids = re.findall(r"linkedin\.com/(?:comm/)?jobs/view/(\d{8,12})", html)
    urls = [f"https://www.linkedin.com/jobs/view/{jid}" for jid in ids]
    cards = {
        f"https://www.linkedin.com/jobs/view/{jid}": pair
        for jid, pair in _linkedin_cards(html).items()
    }
    return _jobs_from_urls(urls, "linkedin", subject, cards=cards)


# ── NoFluffJobs ───────────────────────────────────────────────────────────────
# URLs: https://nofluffjobs.com/pl/job/some-job-slug-city


@register("nofluffjobs.com")
def parse_nofluffjobs(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r"https://nofluffjobs\.com/pl/job/[\w-]+", html)
    return _jobs_from_urls(urls, "nofluffjobs", subject)


# ── JustJoin.it ───────────────────────────────────────────────────────────────
# URLs: https://justjoin.it/offers/some-offer-slug
#       https://justjoin.it/job-offer/some-offer-slug


@register("justjoin.it")
def parse_justjoin(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r"https://justjoin\.it/(?:offers|job-offer)/[\w-]+", html)
    return _jobs_from_urls(urls, "justjoin", subject)


# ── Bulldogjob ────────────────────────────────────────────────────────────────
# URLs: https://bulldogjob.pl/companies/jobs/XXXXXXXX-some-title


@register("bulldogjob.pl")
def parse_bulldogjob(subject: str, body_text: str, body_html: str) -> list[Job]:
    html = body_html or body_text or ""
    urls = re.findall(r"https://bulldogjob\.(?:pl|com)/companies/jobs/[\w-]+", html)
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
    direct = re.findall(r'https://(?:www\.)?pracuj\.pl/praca/[^">\s]+,oferta,\d+[^">\s]*', html)
    clean = []
    for url in direct:
        if not re.search(r"/praca/[^/]+,oferta,\d+", url):
            continue
        url = re.sub(r"\?.*$", "", url)
        url = re.sub(r"^https://pracuj\.pl/", "https://www.pracuj.pl/", url)
        clean.append(url)
    return _jobs_from_urls(clean, "pracuj", subject)
