"""Regression test for the scout-relay URL/LinkedInSource domain collision.

The synthetic dedup-key URL used to be "https://linkedin.com/scout-posts/p...".
It looked distinctive but its *hostname* was still "linkedin.com", so
LinkedInSource.matches_url() (a hostname-only "linkedin.com" in host check)
claimed it too — and LinkedInSource is registered before
LinkedInScoutRelaySource in hunter.sources._fetch_roster(). Any
fetch_job_text(url) call for a scout URL that wasn't short-circuited by
paste_text (a stray /debug_url, or expired_marker's periodic unsent-row scan
— FAIL rows aren't excluded from iter_unsent_rows) got silently routed to the
real LinkedIn fetcher, which tried to fetch a nonexistent linkedin.com path
instead of raising the relay's intended "no fetchable URL" error.

The fix moves the synthetic URL off the linkedin.com host entirely.
"""

from hunter.sources import fetch_job_text
from hunter.sources.linkedin import LinkedInSource
from hunter.sources.linkedin_scout_relay import URL_PREFIX, LinkedInScoutRelaySource
from hunter.validation import SCOUT_POSTS_URL_MARKER


def test_url_prefix_not_on_linkedin_host() -> None:
    assert "linkedin.com" not in URL_PREFIX


def test_linkedin_source_does_not_claim_scout_url() -> None:
    scout_url = f"{URL_PREFIX}deadbeef"
    assert LinkedInSource().matches_url(scout_url) is False


def test_scout_relay_claims_its_own_url() -> None:
    scout_url = f"{URL_PREFIX}deadbeef"
    assert LinkedInScoutRelaySource().matches_url(scout_url) is True


def test_fetch_job_text_dispatches_to_scout_relay_not_linkedin(monkeypatch) -> None:
    """fetch_job_text() must resolve a scout URL to the relay source (which
    raises its own "no fetchable URL" RuntimeError), never to LinkedInSource
    (which would attempt a real, guaranteed-to-fail HTTP fetch)."""
    scout_url = f"{URL_PREFIX}deadbeef"

    def _linkedin_fetch_should_not_be_called(self, url):
        raise AssertionError("LinkedInSource.fetch_text must not be called for a scout URL")

    monkeypatch.setattr(LinkedInSource, "fetch_text", _linkedin_fetch_should_not_be_called)

    try:
        fetch_job_text(scout_url)
    except RuntimeError as e:
        assert "no fetchable URL" in str(e)
    else:
        raise AssertionError("expected RuntimeError from the scout relay's fetch_text")


def test_url_prefix_and_validation_marker_stay_consistent() -> None:
    assert SCOUT_POSTS_URL_MARKER in URL_PREFIX
