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


def test_gmail_bypasses_title_keyword():
    # Gmail source skips the title-keyword gate (alerts pre-filter relevance).
    j = _job("Some Random Role", source="gmail_linkedin")
    assert classify_job(j) != "title_kw"


def test_excluded_level():
    assert classify_job(_job("Angular Intern")) == "level"


def test_location_reject():
    # Non-empty location outside the whitelist → location reason.
    assert classify_job(_job("Angular Developer", location="Berlin")) == "location"


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
