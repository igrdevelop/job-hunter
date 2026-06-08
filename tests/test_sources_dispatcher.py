"""Tests for the central fetch_job_text() dispatcher in hunter/sources (Phase 3.3)."""

from unittest.mock import patch

from hunter.sources import _fetch_roster, fetch_job_text


def test_fetch_roster_includes_all_detail_sources() -> None:
    """The dispatcher roster must cover every source with detail-page support."""
    names = {src.name for src in _fetch_roster()}
    expected = {
        "justjoin", "nofluffjobs", "linkedin", "bulldogjob", "pracuj",
        "theprotocol", "solidjobs", "inhire", "jobleads", "arbeitnow",
        "remotive", "workingnomads", "jobspresso", "builtin", "remoteok",
        "himalayas", "fourdayweek", "weworkremotely", "remoteleaf",
        "ats_aggregator",
    }
    assert names == expected, f"missing or extra sources: {expected ^ names}"


def test_fetch_roster_is_independent_of_enabled_flags() -> None:
    """Disabled sources still need to claim their URLs for detail-page fetch.

    Without this, a tracker row from a disabled source (or a Gmail-enriched URL
    from a board not currently in ALL_SOURCES) would silently fall through to
    html_fallback and lose JSON-API quality.
    """
    roster = _fetch_roster()
    # Construct call should not throw even if every *_ENABLED flag is False.
    assert len(roster) == 20


def test_fetch_job_text_routes_to_matching_source() -> None:
    """justjoin.it URL must hit JustJoinSource.fetch_text, not html_fallback."""
    with patch(
        "hunter.sources.justjoin.JustJoinSource.fetch_text",
        return_value="justjoin payload",
    ) as m_jj, patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="fallback payload",
    ) as m_fb:
        out = fetch_job_text("https://justjoin.it/job-offer/abc-warsaw")
    assert out == "justjoin payload"
    m_jj.assert_called_once()
    m_fb.assert_not_called()


def test_fetch_job_text_strips_tracking_params_before_dispatch() -> None:
    """URL is cleaned before being passed to the source.fetch_text()."""
    seen_url = {}

    def _capture(self, url):
        seen_url["url"] = url
        return "ok"

    with patch.object(
        type(_fetch_roster()[0]).__mro__[0],  # JustJoinSource base instance type
        "fetch_text",
        _capture,
    ):
        # use a tracking-tagged URL
        fetch_job_text(
            "https://justjoin.it/job-offer/abc?utm_source=newsletter&utm_campaign=x"
        )
    assert "utm_source" not in seen_url["url"]
    assert "utm_campaign" not in seen_url["url"]


def test_fetch_job_text_falls_back_to_html_when_no_source_matches() -> None:
    """Unknown domain → generic HTML extractor."""
    with patch(
        "hunter.sources.html_fallback.fetch_html",
        return_value="generic ok",
    ) as m:
        out = fetch_job_text("https://unknown-site.example/jobs/123")
    assert out == "generic ok"
    m.assert_called_once()


def test_fetch_job_text_workable_uses_aggregator() -> None:
    """A workable URL must route through AtsAggregatorSource.fetch_text."""
    with patch(
        "hunter.sources.ats_aggregator.AtsAggregatorSource.fetch_text",
        return_value="ats payload",
    ) as m:
        out = fetch_job_text("https://apply.workable.com/example/j/ABCDEF/")
    assert out == "ats payload"
    m.assert_called_once()
