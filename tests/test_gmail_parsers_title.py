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
