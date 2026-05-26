"""matches_url() tests for sources that use the BaseSource default fetch_text.

Phase 3.2 batch a — trivial wrappers (arbeitnow, remoteleaf, remoteok,
remotive, weworkremotely). These all delegate detail-page fetching to the
generic HTML fallback, so we only test the URL-matching predicate here.
fetch_text behaviour is covered by tests/test_sources_html.py and
tests/test_base_source_fetch_text.py.
"""

from unittest.mock import patch

import pytest

from hunter.sources.arbeitnow import ArbeitnowSource
from hunter.sources.remoteleaf import RemoteleafSource
from hunter.sources.remoteok import RemoteOkSource
from hunter.sources.remotive import RemotiveSource
from hunter.sources.weworkremotely import WeworkremotelySource


@pytest.mark.parametrize(
    "src_cls, positive_urls, negative_urls",
    [
        (
            ArbeitnowSource,
            [
                "https://www.arbeitnow.com/jobs/companies/x/y",
                "https://arbeitnow.com/jobs/1",
            ],
            ["https://example.com/jobs/1", "https://remotive.com/job/1"],
        ),
        (
            RemoteleafSource,
            ["https://remoteleaf.com/company/foo/bar/", "https://www.remoteleaf.com/x"],
            ["https://example.com/x", "https://justjoin.it/jobs/x"],
        ),
        (
            RemoteOkSource,
            ["https://remoteok.com/remote-jobs/123", "https://www.remoteok.com/jobs/abc"],
            ["https://example.com/x", "https://remoteok.io/jobs/abc"],
        ),
        (
            RemotiveSource,
            ["https://remotive.com/remote-jobs/dev/1", "https://www.remotive.com/x"],
            ["https://example.com/x", "https://remoteok.com/jobs/1"],
        ),
        (
            WeworkremotelySource,
            ["https://weworkremotely.com/listings/abc", "https://www.weworkremotely.com/x"],
            ["https://example.com/x", "https://remotive.com/x"],
        ),
    ],
)
def test_matches_url(src_cls, positive_urls, negative_urls) -> None:
    src = src_cls()
    for url in positive_urls:
        assert src.matches_url(url) is True, f"{src.name} should match {url}"
    for url in negative_urls:
        assert src.matches_url(url) is False, f"{src.name} should NOT match {url}"


@pytest.mark.parametrize(
    "src_cls",
    [
        ArbeitnowSource,
        RemoteleafSource,
        RemoteOkSource,
        RemotiveSource,
        WeworkremotelySource,
    ],
)
def test_fetch_text_delegates_to_html_fallback(src_cls) -> None:
    """All five sources should use the default fetch_text (no override)."""
    src = src_cls()
    with patch("hunter.sources.html_fallback.fetch_html", return_value="ok") as m:
        out = src.fetch_text("https://example.com/whatever")
    assert out == "ok"
    m.assert_called_once_with("https://example.com/whatever")
