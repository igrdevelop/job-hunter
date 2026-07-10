"""Fixes for the three LinkedIn Scout relay bugs (PR #123 review + follow-up).

Issue #142 — get_failed_jobs() must not return scout relay rows: their
synthetic URL is a dedup key, not fetchable, and the retry-rebuilt Job has no
raw["post_text"], so every retry fails by design.

Issue #143 — MIN_JOB_TEXT_LEN=300 silently rejected most real scout posts
(typical LinkedIn hiring posts are well under 300 chars); the scout paste
path gets a lower floor, fetched postings keep 300.

Issue #144 — the old fragment-based URL_PREFIX ("...scout-posts/#p<hash>")
collapsed to ONE url_norm for every scout job (normalize_url strips
fragments), so dedup dropped every candidate after the first. The hash now
lives in the path.
"""

from unittest.mock import patch

import pytest

from hunter.models import Job
from hunter.validation import (
    MIN_JOB_TEXT_LEN,
    MIN_SCOUT_TEXT_LEN,
    SCOUT_POSTS_URL_MARKER,
    is_job_text_too_short,
    min_job_text_len_for,
)

SCOUT_URL = "https://linkedin.com/scout-posts/p1a2b3c4d5e6f7a8b"
# Pre-#144 rows in prod: fragment stripped by normalize_url at write time.
LEGACY_SCOUT_URL = "https://linkedin.com/scout-posts"
BOARD_URL = "https://justjoin.it/job-offer/acme-senior-angular"

# A realistic scout catch: a genuine hiring post, well under 300 chars.
SHORT_POST = (
    "We're hiring a Senior Angular developer! Remote (EU), B2B. "
    "Our team builds fintech dashboards (Angular 17, NgRx, RxJS). "
    "DM me or comment below."
)


def _job(url: str, company: str = "Acme", title: str = "Angular Dev") -> Job:
    return Job(title=title, company=company, location="Remote",
               salary=None, url=url, source="test")


# ── #143: min_job_text_len_for ────────────────────────────────────────────────

def test_scout_url_gets_lower_floor() -> None:
    assert min_job_text_len_for(SCOUT_URL) == MIN_SCOUT_TEXT_LEN


def test_normal_url_keeps_default_floor() -> None:
    assert min_job_text_len_for(BOARD_URL) == MIN_JOB_TEXT_LEN


def test_none_url_keeps_default_floor() -> None:
    assert min_job_text_len_for("") == MIN_JOB_TEXT_LEN


def test_scout_floor_is_lower_than_default() -> None:
    assert MIN_SCOUT_TEXT_LEN < MIN_JOB_TEXT_LEN


def test_realistic_short_post_passes_scout_floor_fails_default() -> None:
    assert len(SHORT_POST) < MIN_JOB_TEXT_LEN
    assert not is_job_text_too_short(SHORT_POST, min_job_text_len_for(SCOUT_URL))
    assert is_job_text_too_short(SHORT_POST, min_job_text_len_for(BOARD_URL))


def test_marker_stays_consistent_with_relay_url_prefix() -> None:
    """Drift guard: the marker in hunter.validation must match the relay's
    URL_PREFIX — they are defined separately to keep validation a leaf module."""
    from hunter.sources.linkedin_scout_relay import URL_PREFIX
    assert SCOUT_POSTS_URL_MARKER in URL_PREFIX


# ── #143: apply_api wiring (Step 1.5a) ────────────────────────────────────────

def _run_main_api(url: str, paste_text: str):
    from hunter.apply_api import main_api
    with patch("hunter.apply_api._already_processed", return_value=False), \
         patch("hunter.apply_api.notify"), \
         patch("hunter.expired_check.is_job_expired", return_value=True), \
         patch("hunter.tracker.add_expired"):
        return main_api(url, paste_text=paste_text, skip_dedup=True)


def test_apply_api_short_scout_paste_passes_length_gate() -> None:
    """A short scout post must clear Step 1.5a and reach the expired check
    (patched to True → main_api returns None instead of sys.exit at 1.5a)."""
    assert _run_main_api(SCOUT_URL, SHORT_POST) is None


def test_apply_api_short_text_on_normal_url_still_aborts() -> None:
    """The same short text on a non-scout URL keeps the 300-char abort."""
    with pytest.raises(SystemExit):
        _run_main_api(BOARD_URL, SHORT_POST)


# ── #142: get_failed_jobs excludes scout relay rows ───────────────────────────

def test_get_failed_jobs_excludes_scout_rows(tracker_db) -> None:
    from hunter import tracker
    tracker.add_failed(_job(SCOUT_URL))
    tracker.add_failed(_job(BOARD_URL, company="Other Co", title="Frontend Dev"))

    urls = {j.url for j in tracker.get_failed_jobs()}
    assert not any(SCOUT_POSTS_URL_MARKER in u for u in urls)
    assert any("justjoin.it" in u for u in urls)


def test_get_failed_jobs_scout_row_still_recorded(tracker_db) -> None:
    """The FAIL row itself is kept (dedup must still see it) — only the
    retry loop skips it."""
    from hunter import tracker
    tracker.add_failed(_job(SCOUT_URL))
    assert tracker.is_known(SCOUT_URL, "Acme", "Angular Dev")
    assert tracker.get_failed_jobs() == []


def test_get_failed_jobs_excludes_legacy_collapsed_scout_rows(tracker_db) -> None:
    """Rows written before #144 have the bare collapsed URL (fragment stripped
    at write time) — the marker must exclude those too."""
    from hunter import tracker
    tracker.add_failed(_job(LEGACY_SCOUT_URL))
    assert tracker.get_failed_jobs() == []


# ── #144: scout URLs must survive normalization distinctly ────────────────────

def test_scout_urls_normalize_distinctly() -> None:
    from hunter.sources.linkedin_scout_relay import URL_PREFIX
    from hunter.tracker import normalize_url
    n1 = normalize_url(f"{URL_PREFIX}aaaa1111bbbb2222")
    n2 = normalize_url(f"{URL_PREFIX}cccc3333dddd4444")
    assert n1 != n2
    # and the hash actually survives (fragment-based keys lost it entirely)
    assert "aaaa1111bbbb2222" in n1


def test_second_scout_candidate_not_deduped_after_first_tracked(tracker_db) -> None:
    """Regression for the real failure mode: candidate 2 must not be 'known'
    just because candidate 1 was tracked."""
    from hunter import tracker
    from hunter.sources.linkedin_scout_relay import LinkedInScoutRelaySource

    rec1 = {"author": "Anna K", "body": "We're hiring an Angular dev! " * 5}
    rec2 = {"author": "Piotr Z", "body": "Looking for a frontend engineer! " * 5}
    job1 = LinkedInScoutRelaySource._record_to_job(rec1)
    job2 = LinkedInScoutRelaySource._record_to_job(rec2)
    assert job1.url != job2.url

    tracker.add_failed(job1)
    assert tracker.is_known(job1.url, job1.company, job1.title)
    assert not tracker.is_known(job2.url, job2.company, job2.title)
