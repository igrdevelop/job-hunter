"""Step 7: stub title is derived from the URL slug, not the email subject.

A pracuj digest's subject can name one job while the body links to another
(recommendations), which used to pair a misleading title with the URL.
"""

from hunter.gmail_parsers import _title_from_url, parse_pracuj, parse_linkedin


def test_title_from_pracuj_slug():
    url = (
        "https://www.pracuj.pl/praca/"
        "programista-programistka-frontend-developer-react-next-js"
        "-w-dziale-produktu-cent-warszawa,oferta,1004813501"
    )
    title = _title_from_url(url)
    assert "React" in title and "Next" in title
    assert "oferta" not in title.lower()
    assert "1004813501" not in title


def test_title_from_bulldogjob_strips_leading_id():
    url = "https://bulldogjob.pl/companies/jobs/abc12345-senior-angular-developer"
    assert _title_from_url(url) == "Senior Angular Developer"


def test_title_from_linkedin_numeric_returns_empty():
    # Numeric id → no useful slug → caller falls back to subject.
    assert _title_from_url("https://www.linkedin.com/jobs/view/1234567890") == ""


def test_parse_pracuj_title_matches_url_not_subject():
    subject = "Angular Developer (K/M/N): pracodawca zakończył rekrutację."
    html = (
        'See this role: '
        '<a href="https://www.pracuj.pl/praca/'
        'frontend-developer-react-next-js-warszawa,oferta,1004813501">link</a>'
    )
    jobs = parse_pracuj(subject, "", html)
    assert len(jobs) == 1
    # Title reflects the actual URL (react), not the misleading Angular subject.
    assert "React" in jobs[0].title
    assert "Angular" not in jobs[0].title


def test_parse_linkedin_falls_back_to_subject():
    subject = "10 new jobs for you"
    html = 'href="https://www.linkedin.com/jobs/view/1234567890"'
    jobs = parse_linkedin(subject, "", html)
    assert len(jobs) == 1
    assert jobs[0].title == subject  # numeric id → fallback to subject


def test_parse_linkedin_extracts_comm_click_tracker_urls():
    # Real LinkedIn digest hrefs go through the /comm/ click-tracker with
    # refId/trk query params. The parser must canonicalize to /jobs/view/<id>.
    subject = "New jobs similar to Senior Frontend Engineer at Dev.Pro"
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/4123456789/'
        '?refId=abc&trk=eml-jymbii-organic-job-card">Angular Tech Lead</a>'
        '<a href="https://www.linkedin.com/comm/jobs/view/4987654321/'
        '?refId=def&trk=eml-jymbii-organic-job-card">Senior Frontend Engineer</a>'
    )
    jobs = parse_linkedin(subject, "", html)
    assert [j.url for j in jobs] == [
        "https://www.linkedin.com/jobs/view/4123456789",
        "https://www.linkedin.com/jobs/view/4987654321",
    ]


def test_parse_pracuj_bare_host_in_digest_email():
    # Real pracuj recommendation digest (rekomendacje@wysylka.pracuj.pl) uses
    # the bare-host variant https://pracuj.pl/praca/...,oferta,X (no www.) with
    # sendid/utm trackers. Old regex required www. and lost 20 vacancies per
    # email. New regex accepts both; canonicalize to www. for dedup stability.
    subject = "Frontend Developer (K/M) - oferta 18.06.2026"
    html = (
        '<a href="https://pracuj.pl/praca/frontend-developer-k-m-warszawa,oferta,1004881674'
        '?sendid=abc&utm_source=rekomendacje">a</a>'
        '<a href="https://pracuj.pl/praca/react-developer-warszawa,oferta,1004870417'
        '?sendid=abc&utm_source=rekomendacje">b</a>'
        # canonical www. form coming through the same email — must dedup
        '<a href="https://www.pracuj.pl/praca/react-developer-warszawa,oferta,1004870417">b2</a>'
    )
    jobs = parse_pracuj(subject, "", html)
    assert [j.url for j in jobs] == [
        "https://www.pracuj.pl/praca/frontend-developer-k-m-warszawa,oferta,1004881674",
        "https://www.pracuj.pl/praca/react-developer-warszawa,oferta,1004870417",
    ]


def test_parse_pracuj_rejects_slugless_url():
    # /praca/oferta,ID without a title slug is a 404 on pracuj.pl.
    html = '<a href="https://pracuj.pl/praca/oferta,1004881674?sendid=x">no slug</a>'
    assert parse_pracuj("subj", "", html) == []


def test_parse_linkedin_mixed_comm_and_bare_dedup():
    # Same id reached via both /comm/jobs/view/ and /jobs/view/ collapses to one row.
    subject = "Senior Frontend Engineer at ClickUp"
    html = (
        '<a href="https://www.linkedin.com/comm/jobs/view/4111111111/?trk=eml">a</a>'
        '<a href="https://www.linkedin.com/jobs/view/4111111111">b</a>'
    )
    jobs = parse_linkedin(subject, "", html)
    assert len(jobs) == 1
    assert jobs[0].url == "https://www.linkedin.com/jobs/view/4111111111"
