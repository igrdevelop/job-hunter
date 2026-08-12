"""LinkedIn alert digests: per-job title/company instead of the email subject.

A LinkedIn job-alert email links the job named in its subject PLUS several
"similar" ones. `parse_linkedin` used to label every one of them with the
subject, which on 2026-08-12 sent two off-stack roles all the way through
generation: a "Software Engineer" (LanceSoft, Java/React contract) arrived
titled "Front-End Developer (Agentic AI) at Comarch", and a "Full stack /
Front-end developer - Scala / Java" (Luxoft) as "Programista Frontend
(Angular)". Both passed the title whitelist on a title belonging to another
vacancy, and the shared `[linkedin]` company made every job of one email
collapse onto a single company+title dedup key.

The markup below mirrors the real emails (verified against 8 live alerts,
31/31 cards parsed): an <a> to /jobs/view/<id> whose text is the title,
followed by a "<Company> · <City> (Remote|Hybrid)" line.
"""

from hunter.gmail_parsers import _linkedin_cards, parse_linkedin
from hunter.tracker import dedup_key


def _card(job_id: str, title: str, company: str, location: str) -> str:
    return (
        f'<td><a href="https://www.linkedin.com/comm/jobs/view/{job_id}/'
        f'?trackingId=abc&amp;trk=eml-email_job_alert_digest_01-primary_job_list-0-company_logo_0"'
        f' target="_blank"><img src="logo.png" /></a></td>'
        f'<td><a href="https://www.linkedin.com/comm/jobs/view/{job_id}/'
        f'?trackingId=abc&amp;trk=eml-email_job_alert_digest_01-primary_job_list-0-jobcard_body_0"'
        f' target="_blank" class="font-bold text-md">\n  {title}\n</a>'
        f'<p class="text-system-gray-100">{company} &middot; {location}</p></td>'
    )


DIGEST_SUBJECT = "Front-End Developer (Agentic AI) at Comarch"
DIGEST_HTML = (
    "<html><body><table>"
    + _card("4424775650", "Front-End Developer (Agentic AI)", "Comarch", "Bielsko-Biała (Hybrid)")
    + _card("4451158730", "Software Engineer", "LanceSoft Europe", "Gdańsk (Remote)")
    + "</table></body></html>"
)


def test_cards_carry_their_own_title_and_company() -> None:
    jobs = parse_linkedin(DIGEST_SUBJECT, "", DIGEST_HTML)
    assert [(j.title, j.company) for j in jobs] == [
        ("Front-End Developer (Agentic AI)", "Comarch"),
        ("Software Engineer", "LanceSoft Europe"),
    ]


def test_similar_job_does_not_inherit_the_subject_title() -> None:
    """The exact 2026-08-12 case: the LanceSoft URL must NOT be titled with
    the Comarch subject — that title is what let it past the whitelist."""
    jobs = parse_linkedin(DIGEST_SUBJECT, "", DIGEST_HTML)
    lancesoft = next(j for j in jobs if j.url.endswith("4451158730"))
    assert lancesoft.title == "Software Engineer"
    assert DIGEST_SUBJECT not in lancesoft.title


def test_real_titles_survive_the_listing_filter_check() -> None:
    """End-to-end point of the fix: with its own title the off-stack job is
    filtered, with the subject title it passed."""
    from hunter.filters import classify_job

    jobs = parse_linkedin(DIGEST_SUBJECT, "", DIGEST_HTML)
    lancesoft = next(j for j in jobs if j.url.endswith("4451158730"))
    assert classify_job(lancesoft) == "title_kw"


def test_each_card_gets_its_own_dedup_key() -> None:
    """Shared [linkedin] company + shared subject title made dedup_key
    identical for every job of one email, so the hunt dropped all but the
    first as "Dup company+title" (25 such discards in one 19h window)."""
    jobs = parse_linkedin(DIGEST_SUBJECT, "", DIGEST_HTML)
    keys = {dedup_key(j.company, j.title) for j in jobs}
    assert len(keys) == len(jobs)


def test_untitled_cards_fall_back_to_subject_with_unique_stub_company() -> None:
    """Fallback path (markup changed, no card text): the subject is still the
    only title available, but the stub company keeps the jobs distinct."""
    html = (
        '<a href="https://www.linkedin.com/jobs/view/4111111111">a</a>'
        '<a href="https://www.linkedin.com/jobs/view/4222222222">b</a>'
    )
    jobs = parse_linkedin("10 new jobs for you", "", html)
    assert [j.title for j in jobs] == ["10 new jobs for you"] * 2
    assert [j.company for j in jobs] == ["[linkedin]#4111111111", "[linkedin]#4222222222"]
    assert len({dedup_key(j.company, j.title) for j in jobs}) == 2


def test_logo_anchor_without_text_does_not_win_over_the_title() -> None:
    cards = _linkedin_cards(DIGEST_HTML)
    assert cards["4451158730"][0] == "Software Engineer"


def test_missing_company_line_leaves_the_plain_stub() -> None:
    html = (
        '<a href="https://www.linkedin.com/jobs/view/4333333333">Senior Angular Developer</a>'
        "<p>no separator here</p>"
    )
    jobs = parse_linkedin("subject", "", html)
    assert jobs[0].title == "Senior Angular Developer"
    # A real title already makes the dedup key unique — no tag needed.
    assert jobs[0].company == "[linkedin]"


def test_absurdly_long_company_text_is_rejected() -> None:
    html = (
        '<a href="https://www.linkedin.com/jobs/view/4444444444">Angular Developer</a>'
        "<p>" + "x" * 200 + " &middot; Warsaw</p>"
    )
    jobs = parse_linkedin("subject", "", html)
    assert jobs[0].company == "[linkedin]"
