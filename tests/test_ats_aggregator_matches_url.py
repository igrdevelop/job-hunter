"""matches_url() tests for the ATS aggregator (Phase 3.2b).

Covers all five ATS provider domain patterns. fetch_text uses the BaseSource
default — covered separately in tests/test_base_source_fetch_text.py.
"""

from unittest.mock import patch

import pytest

from hunter.sources.ats_aggregator import AtsAggregatorSource


SRC = AtsAggregatorSource()


@pytest.mark.parametrize(
    "url",
    [
        "https://apply.workable.com/example-company/j/ABC123/",
        "https://boards.greenhouse.io/example/jobs/12345",
        "https://example.greenhouse.io/jobs/12345",
        "https://jobs.lever.co/example/abc-def-123",
        "https://example.recruitee.com/o/senior-frontend",
        "https://jobs.ashbyhq.com/example/abc-def",
    ],
)
def test_matches_url_positive(url: str) -> None:
    assert SRC.matches_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/jobs/123",
        "https://justjoin.it/job-offer/x",
        "https://nofluffjobs.com/job/x",
        # Recruitee carve-out: only match `*.recruitee.com`, not bare hostname
        "https://recruitee.com/jobs/x",
        # lever marketing site (we only care about jobs subdomain)
        "https://www.lever.co/about",
    ],
)
def test_matches_url_negative(url: str) -> None:
    assert SRC.matches_url(url) is False


def test_fetch_text_delegates_to_html_fallback() -> None:
    with patch("hunter.sources.html_fallback.fetch_html", return_value="ok") as m:
        out = SRC.fetch_text("https://apply.workable.com/example/j/ABC123/")
    assert out == "ok"
    m.assert_called_once_with("https://apply.workable.com/example/j/ABC123/")
