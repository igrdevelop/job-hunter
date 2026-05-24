"""Tests for Gmail location check — no bypass after П-3.2 follow-up.

Gmail-sourced jobs used to skip _matches_location entirely.
After the fix they go through the standard whitelist check — the same
one applied to LinkedIn, JustJoin, etc.
"""
from unittest.mock import patch

import pytest

from hunter.models import Job
from hunter.filters import apply_filters_with_stats


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _gmail_job(title: str, location: str, source: str = "gmail_linkedin") -> Job:
    return Job(
        title=title,
        company="AcmeCo",
        location=location,
        salary=None,
        url="https://example.com/job",
        source=source,
    )


_PATCH_FILTER = {
    "title_keywords": ["angular", "frontend", "react"],
    "exclude_levels": ["intern", "junior"],
    "exclude_patterns": [],
    "locations": ["remote", "wrocław", "wroclaw"],
    "require_angular": False,
    "exclude_react_without_angular": False,
    "exclude_german_language_required": False,
}


# ---------------------------------------------------------------------------
# Gmail jobs with location must satisfy the whitelist
# ---------------------------------------------------------------------------

def test_gmail_remote_location_passes():
    """Gmail job with location='Remote' should pass location check."""
    job = _gmail_job("Angular Developer", "Remote")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result, "remote Gmail job should pass"
    assert reasons["location"] == 0


def test_gmail_wroclaw_location_passes():
    """Gmail job with Wrocław in location passes."""
    job = _gmail_job("Senior Angular Developer", "Wrocław, Poland")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result, "Wrocław Gmail job should pass"
    assert reasons["location"] == 0


def test_gmail_remote_in_title_passes():
    """Gmail job with 'Remote' in title and non-specific location passes."""
    job = _gmail_job("Remote Angular Developer", "Poland")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result, "Remote-in-title Gmail job should pass"
    assert reasons["location"] == 0


def test_gmail_wroclaw_in_title_passes():
    """'Wrocław' appearing only in title (location='Poland') should pass."""
    job = _gmail_job("Angular Developer – Wrocław", "Poland")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result, "Wrocław-in-title Gmail job should pass"
    assert reasons["location"] == 0


def test_gmail_krakow_location_rejected():
    """Gmail job with Kraków location (no remote/wrocław) is now rejected."""
    job = _gmail_job("Angular Developer", "Kraków")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result, "Kraków Gmail job should be rejected"
    assert reasons["location"] == 1


def test_gmail_warsaw_location_rejected():
    """Gmail job with Warsaw location is rejected."""
    job = _gmail_job("Frontend Developer (Angular)", "Warszawa / Warsaw")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result, "Warsaw Gmail job should be rejected"
    assert reasons["location"] == 1


def test_gmail_empty_location_rejected():
    """Gmail job with empty location (no title signal) is rejected by strict whitelist."""
    job = _gmail_job("Angular Developer", "")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result, "No-location Gmail job with no geo signal should be rejected"
    assert reasons["location"] == 1


def test_gmail_empty_location_remote_title_passes():
    """Empty location but 'remote' in title → passes (title is part of the blob)."""
    job = _gmail_job("Angular Developer – Remote", "")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result, "Remote-in-title Gmail job with empty location should pass"
    assert reasons["location"] == 0


def test_gmail_krakow_in_title_rejected():
    """LinkedIn puts city in title ('Angular Dev Kraków'); email alert picks it up;
    should be rejected even if location field says 'Poland'."""
    job = _gmail_job("Angular Developer Kraków Zabłocie", "Poland")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result, "Kraków-in-title Gmail job should be rejected"
    assert reasons["location"] == 1


def test_gmail_source_gmail_nfj_also_filtered():
    """Different gmail_* source names all go through location check."""
    job = _gmail_job("Angular Developer", "Warszawa", source="gmail_nfj")
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result
    assert reasons["location"] == 1


def test_non_gmail_krakow_still_rejected():
    """Regression: non-gmail source with Kraków is still rejected (no regression)."""
    job = Job(
        title="Angular Developer",
        company="AcmeCo",
        location="Kraków",
        salary=None,
        url="https://example.com/job2",
        source="justjoin",
    )
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert not result
    assert reasons["location"] == 1


def test_non_gmail_remote_still_passes():
    """Regression: non-gmail Remote job still passes location check."""
    job = Job(
        title="Angular Developer",
        company="AcmeCo",
        location="Remote",
        salary=None,
        url="https://example.com/job3",
        source="justjoin",
    )
    with patch("hunter.filters.FILTER", _PATCH_FILTER):
        result, reasons = apply_filters_with_stats([job])
    assert result
    assert reasons["location"] == 0
