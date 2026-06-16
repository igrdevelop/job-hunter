"""B3 — tracker must persist normalised URLs so dedup is param-order-independent."""


from hunter.tracker import (
    add_applied,
    add_skipped,
    add_failed,
    add_expired,
    get_known_urls,
    normalize_url,
    lookup_url,
)
from hunter.models import Job


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_content(url: str, company: str = "Acme", title: str = "Angular Dev") -> dict:
    return {
        "company_name": company,
        "job_title": title,
        "stack": "Angular",
        "apply_url": url,
        "output_folder": "Applications/2099-01-01/Acme",
        "ats_score": "95",
        "cover_letter": "Dear...",
        "to_learn": "",
    }


def _make_job(url: str, company: str = "Acme", title: str = "Angular Dev") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None,
               url=url, source="test")


# ---------------------------------------------------------------------------
# add_applied writes normalised URL
# ---------------------------------------------------------------------------

def test_add_applied_writes_normalized_url(tracker_db) -> None:
    raw_url = (
        "https://www.pracuj.pl/praca/angular-dev,oferta,1004756554"
        "?sendid=abc123&utm_source=newsletter&sug=xyz"
    )
    expected = normalize_url(raw_url)

    add_applied(_make_content(raw_url))

    rows = lookup_url(raw_url)
    assert rows, "no row written"
    assert rows[0]["ats"] == "95%"
    assert expected in get_known_urls()


def test_add_applied_unknown_param_still_normalized(tracker_db) -> None:
    """Params not in the drop-list must be stripped for pracuj.pl (path-id domain)."""
    raw_url = "https://www.pracuj.pl/praca/angular-dev,oferta,99?jobAlertId=42"
    expected = "https://www.pracuj.pl/praca/angular-dev,oferta,99"

    add_applied(_make_content(raw_url))

    known = get_known_urls()
    assert expected in known, f"stored keys: {known!r}"


def test_add_applied_url_dedup_catches_different_params(tracker_db) -> None:
    """Same job scraped twice with different tracking params must not produce two rows."""
    url_v1 = "https://justjoin.it/job-offer/acme-angular-dev?utm_source=email"
    url_v2 = "https://justjoin.it/job-offer/acme-angular-dev?utm_source=push"

    written1 = add_applied(_make_content(url_v1))
    written2 = add_applied(_make_content(url_v2))

    assert written1 is True
    assert written2 is False, "second apply with same base URL should be rejected"


# ---------------------------------------------------------------------------
# add_skipped writes normalised URL
# ---------------------------------------------------------------------------

def test_add_skipped_writes_normalized_url(tracker_db) -> None:
    raw_url = "https://nofluffjobs.com/pl/job/acme-angular-dev?ref=homepage&utm_campaign=X"
    expected = normalize_url(raw_url)

    add_skipped(_make_job(raw_url))

    known = get_known_urls()
    assert expected in known, f"stored keys: {known!r}"


# ---------------------------------------------------------------------------
# add_failed writes normalised URL
# ---------------------------------------------------------------------------

def test_add_failed_writes_normalized_url(tracker_db) -> None:
    raw_url = "https://justjoin.it/job-offer/acme-dev?trackingId=T99&utm_source=jj"
    expected = normalize_url(raw_url)

    add_failed(_make_job(raw_url))

    known = get_known_urls()
    assert expected in known


# ---------------------------------------------------------------------------
# add_expired writes normalised URL
# ---------------------------------------------------------------------------

def test_add_expired_writes_normalized_url(tracker_db) -> None:
    raw_url = "https://www.pracuj.pl/praca/dev,oferta,5555?sendid=XXX"
    expected = normalize_url(raw_url)

    add_expired(raw_url, company="Acme", title="Angular Dev")

    known = get_known_urls()
    assert expected in known


def test_add_expired_marks_sent_not_ats(tracker_db) -> None:
    """EXPIRED marker lives in Sent; ATS column gets SKIP (no CV generated)."""
    url = "https://justjoin.it/job-offer/acme-frontend-developer-warszawa-javascript"

    add_expired(url, company="Acme", title="Frontend Developer")

    rows = lookup_url(url)
    assert len(rows) == 1
    assert rows[0]["sent"] == "EXPIRED"
    assert rows[0]["ats"] == "SKIP"


# ---------------------------------------------------------------------------
# get_known_urls — round-trip sanity
# ---------------------------------------------------------------------------

def test_get_known_urls_matches_normalized_lookup(tracker_db) -> None:
    """After write, get_known_urls must return the same key used by the hunt loop."""
    raw_url = "https://www.pracuj.pl/praca/test,oferta,1234?utm_source=alert&sendid=9"

    add_applied(_make_content(raw_url))
    known = get_known_urls()

    # The hunt loop does: normalize_url(j.url) in known_urls
    assert normalize_url(raw_url) in known
    # Must also match a re-scrape with different params
    rescrape_url = "https://www.pracuj.pl/praca/test,oferta,1234?utm_source=push&sendid=99"
    assert normalize_url(rescrape_url) in known
