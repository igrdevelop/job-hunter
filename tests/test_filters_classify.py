"""Phase B: classify_job() returns the per-job filter reason.

apply_filters_with_stats() is now a thin aggregator over classify_job(); these
tests pin the per-job reason vocabulary the Gmail report relies on.
"""

from hunter.filters import classify_job, apply_filters_with_stats, FILTER_REASONS
from hunter.models import Job


def _job(title: str, location: str = "Remote", source: str = "justjoin", **kw) -> Job:
    return Job(
        title=title, company=kw.get("company", "Acme"), location=location,
        salary=None, url=kw.get("url", f"https://x/{title}"), source=source,
        raw=kw.get("raw", {}),
    )


def test_passing_job_returns_none():
    assert classify_job(_job("Senior Angular Developer")) is None


def test_non_gmail_title_keyword_miss():
    assert classify_job(_job("Plumber")) == "title_kw"


def test_gmail_enforces_title_keyword():
    # Recommendation digests (pracuj rekomendacje@, nofluff "similar offers",
    # linkedin "New jobs similar to ...") pack 10–20 unrelated roles next to
    # the headline FE one. Gmail sources must go through the title whitelist
    # like every other source, or AUTO_APPLY burns LLM calls on .NET / PHP /
    # database / DevOps roles bundled into a "Frontend Engineer III ..." email.
    assert classify_job(_job("Programista baz danych", source="gmail_pracuj")) == "title_kw"
    assert classify_job(_job("Database Developer", source="gmail_pracuj")) == "title_kw"
    assert classify_job(_job("Senior Go Developer", source="gmail_linkedin")) == "title_kw"
    # Genuine FE titles in gmail still pass the title gate.
    assert classify_job(_job("Senior Frontend Engineer", source="gmail_linkedin")) is None
    assert classify_job(_job("Angular Developer", source="gmail_pracuj")) is None


def test_excluded_level():
    assert classify_job(_job("Angular Intern")) == "level"


def test_location_reject():
    # Non-empty location outside the whitelist → location reason.
    assert classify_job(_job("Angular Developer", location="Berlin")) == "location"


def test_russia_location_rejected_even_when_remote():
    # Owner decision 2026-07-12: skip Russia-tied roles outright, even
    # remote ones — checked before the generic location whitelist, so it
    # fires with its own "russia" reason rather than "location".
    assert classify_job(_job("Angular Developer", location="Remote · Russia")) == "russia"


def test_russia_title_rejected_even_with_remote_location():
    assert classify_job(_job("Angular Developer — Russia", location="Remote")) == "russia"


def test_russia_cyrillic_location_rejected():
    assert classify_job(_job("Angular Developer", location="Удалённо, РФ")) == "russia"
    assert classify_job(_job("Angular Developer", location="Россия")) == "russia"


def test_reason_in_vocabulary():
    r = classify_job(_job("Plumber"))
    assert r in FILTER_REASONS


def test_aggregate_matches_classify():
    jobs = [
        _job("Senior Angular Developer"),          # pass
        _job("Plumber"),                           # title_kw
        _job("Angular Intern"),                    # level
        _job("Angular Developer", location="Berlin"),  # location
    ]
    passed, reasons = apply_filters_with_stats(jobs)
    assert len(passed) == 1
    assert reasons["title_kw"] == 1
    assert reasons["level"] == 1
    assert reasons["location"] == 1
