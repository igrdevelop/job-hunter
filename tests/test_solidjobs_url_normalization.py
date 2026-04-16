from hunter.sources.solidjobs import SolidJobsSource
from job_fetch.solidjobs import normalize_solidjobs_offer_url


def test_job_fetch_normalizes_rss_offer_url() -> None:
    raw = "https://solid.jobs/o/6mksvv7r/rss"
    assert normalize_solidjobs_offer_url(raw) == "https://solid.jobs/o/6mksvv7r"


def test_job_fetch_keeps_non_rss_solidjobs_url() -> None:
    raw = "https://solid.jobs/offer/12345/frontend-engineer"
    assert normalize_solidjobs_offer_url(raw) == raw


def test_solidjobs_source_parse_normalizes_rss_url() -> None:
    src = SolidJobsSource()
    raw = {
        "title": "Frontend Developer (Angular)",
        "company": "Acme",
        "location": "Wroclaw",
        "salary": "18 000 - 22 000 PLN",
        "categories": ["Angular", "TypeScript"],
        "url": "https://solid.jobs/o/6mksvv7r/rss?utm_source=rss",
    }
    job = src._parse(raw)
    assert job is not None
    assert job.url == "https://solid.jobs/o/6mksvv7r"
