"""Remote OK source parsing (no network)."""

from hunter.sources.remoteok import RemoteOkSource, _extract_job_rows, _format_salary


def test_remoteok_skips_metadata_row() -> None:
    rows = _extract_job_rows(
        [
            {"last_updated": 1, "legal": "x"},
            {
                "slug": "remote-dev-acme-1",
                "position": "Frontend Developer",
                "company": "Acme",
                "location": "Remote",
                "description": "<p>Angular</p>",
                "tags": ["javascript"],
                "salary_min": 80000,
                "salary_max": 120000,
            },
        ]
    )
    assert len(rows) == 1
    assert rows[0]["slug"] == "remote-dev-acme-1"


def test_remoteok_parse() -> None:
    src = RemoteOkSource()
    raw = {
        "slug": "remote-dev-acme-1",
        "position": "Frontend Developer",
        "company": "Acme",
        "location": "Remote",
        "description": "<p>TypeScript</p>",
        "tags": ["angular"],
        "salary_min": 90000,
        "salary_max": 0,
    }
    job = src._parse(raw)
    assert job is not None
    assert job.title == "Frontend Developer"
    assert job.company == "Acme"
    assert job.location == "Remote"
    assert job.salary == "$90 000+ USD/yr"
    assert job.url == "https://remoteok.com/remote-jobs/remote-dev-acme-1"
    assert job.source == "remoteok"


def test_remoteok_parse_incomplete() -> None:
    src = RemoteOkSource()
    assert src._parse({"slug": "x", "position": "", "company": "A"}) is None


def test_format_salary_empty() -> None:
    assert _format_salary({"salary_min": 0, "salary_max": 0}) is None


def test_format_salary_range() -> None:
    assert _format_salary({"salary_min": 30000, "salary_max": 40000}) == "$30 000–$40 000 USD/yr"
