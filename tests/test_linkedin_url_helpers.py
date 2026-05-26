"""URL parsing helpers ported into hunter/sources/linkedin.py from job_fetch/linkedin_parse.py."""

from hunter.sources.linkedin import (
    is_linkedin_search,
    is_linkedin_url,
    is_linkedin_view,
    job_view_url,
    normalize_linkedin_url,
    parse_linkedin_job_ids,
)


def test_is_linkedin_url() -> None:
    assert is_linkedin_url("https://www.linkedin.com/jobs/view/123")
    assert is_linkedin_url("https://linkedin.com/x")
    assert not is_linkedin_url("https://example.com/x")


def test_is_linkedin_view() -> None:
    assert is_linkedin_view("https://www.linkedin.com/jobs/view/123")
    assert not is_linkedin_view("https://www.linkedin.com/jobs/search/?q=x")
    assert not is_linkedin_view("https://example.com/x")


def test_is_linkedin_search() -> None:
    assert is_linkedin_search(
        "https://www.linkedin.com/jobs/search/?currentJobId=123"
    )
    assert not is_linkedin_search("https://www.linkedin.com/jobs/view/123")
    assert not is_linkedin_search("https://example.com/jobs/search/")


def test_parse_linkedin_job_ids_extracts_currentJobId() -> None:
    url = "https://www.linkedin.com/jobs/search/?currentJobId=4012345"
    assert parse_linkedin_job_ids(url) == ["4012345"]


def test_parse_linkedin_job_ids_extracts_originToLandingJobPostings_list() -> None:
    url = (
        "https://www.linkedin.com/jobs/search/?currentJobId=111"
        "&originToLandingJobPostings=222%2C333%2C444"
    )
    assert parse_linkedin_job_ids(url) == ["111", "222", "333", "444"]


def test_parse_linkedin_job_ids_dedups() -> None:
    url = (
        "https://www.linkedin.com/jobs/search/?currentJobId=111"
        "&originToLandingJobPostings=111%2C222"
    )
    assert parse_linkedin_job_ids(url) == ["111", "222"]


def test_parse_linkedin_job_ids_returns_empty_when_no_ids() -> None:
    assert parse_linkedin_job_ids("https://www.linkedin.com/jobs/search/") == []


def test_job_view_url() -> None:
    assert job_view_url("123456") == "https://www.linkedin.com/jobs/view/123456/"


def test_normalize_linkedin_url_strips_tracking() -> None:
    raw = "https://www.linkedin.com/jobs/view/123456/?trk=abc&refId=xyz"
    assert normalize_linkedin_url(raw) == "https://www.linkedin.com/jobs/view/123456/"


def test_normalize_linkedin_url_passthrough_for_non_view_urls() -> None:
    raw = "https://www.linkedin.com/jobs/search/?currentJobId=42"
    assert normalize_linkedin_url(raw) == raw
    assert normalize_linkedin_url("https://example.com/x") == "https://example.com/x"
