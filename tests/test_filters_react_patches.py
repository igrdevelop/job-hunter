"""
Tests for P-3.x: React-only filter fixes.

P-3.1: _is_react_only_title() — title-based check for all sources
P-3.2: Gmail bypass no longer silences exclude_pattern / react checks
P-3.3: react native added to exclude_patterns
"""
import pytest
from hunter.filters import apply_filters, apply_filters_with_stats, _is_react_only_title
from hunter.models import Job


def _job(*, title: str, location: str = "Wroclaw", source: str = "test",
         raw: dict | None = None) -> Job:
    return Job(
        title=title, company="Acme", location=location, salary=None,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        source=source, raw=raw or {},
    )


def _gmail_job(*, title: str, location: str = "remote") -> Job:
    return _job(title=title, location=location, source="gmail_linkedin")


# ---------------------------------------------------------------------------
# P-3.1 — _is_react_only_title unit tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "React Developer",
    "React Engineer",
    "React Native Engineer",
    "React.js Developer",
    "Frontend Developer (React)",
    "Software Engineer React",
])
def test_is_react_only_title_positive(title: str) -> None:
    assert _is_react_only_title(title), f"Expected True for: {title!r}"


@pytest.mark.parametrize("title", [
    "Angular Developer",
    "Senior Frontend Developer (Angular/React)",  # angular present → False
    "Frontend Developer",                          # no react mention → False
    "React + Angular Developer",                   # angular present → False
])
def test_is_react_only_title_negative(title: str) -> None:
    assert not _is_react_only_title(title), f"Expected False for: {title!r}"


# ---------------------------------------------------------------------------
# P-3.1 — applied via apply_filters for regular sources
# ---------------------------------------------------------------------------

def test_react_developer_title_blocked_for_regular_source() -> None:
    jobs = [_job(title="React Developer")]
    result = apply_filters(jobs)
    assert result == []


def test_react_native_engineer_blocked() -> None:
    jobs = [_job(title="React Native Engineer")]
    result = apply_filters(jobs)
    assert result == []


# ---------------------------------------------------------------------------
# P-3.2 — Gmail bypass no longer silences React / exclude-pattern checks
# ---------------------------------------------------------------------------

def test_react_developer_blocked_even_from_gmail() -> None:
    """Gmail source must NOT bypass React-only title check.

    Title carries a whitelist keyword (frontend) so it passes the title_kw gate
    and reaches the react_no_angular check — that's the filter we're exercising.
    """
    job = _gmail_job(title="Frontend Developer (React)")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["react_no_angular"] == 1


def test_react_native_blocked_from_gmail_via_exclude_pattern() -> None:
    """React Native in Gmail title → exclude_pattern fires."""
    job = _gmail_job(title="Senior Frontend React Native Engineer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []


def test_magento_blocked_from_gmail() -> None:
    """Magento in Gmail title → exclude_pattern must fire (was bypassed before P-3.2)."""
    job = _gmail_job(title="Frontend Magento Developer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["exclude_pattern"] == 1


def test_angular_developer_gmail_warsaw_now_rejected() -> None:
    """Gmail jobs now go through the standard location check (bypass removed).
    Warsaw (anti-hybrid city, not in allowed whitelist) → rejected.
    """
    job = _gmail_job(title="Senior Angular Developer", location="Warsaw")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["location"] == 1


def test_angular_developer_gmail_remote_passes() -> None:
    """Gmail Angular job with Remote location passes after location bypass removal."""
    job = _gmail_job(title="Senior Angular Developer", location="remote")
    result = apply_filters(jobs=[job])
    assert len(result) == 1


def test_junior_blocked_from_gmail() -> None:
    """Level exclusion (junior) must apply to Gmail sources — regression guard."""
    job = _gmail_job(title="Junior Angular Developer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["level"] == 1


# ---------------------------------------------------------------------------
# P-3.3 — React Native in exclude_patterns
# (Use gmail source so title_kw is bypassed and we test the real target filter)
# ---------------------------------------------------------------------------

def test_react_native_blocked_via_gmail() -> None:
    """React Native in a Gmail job title is blocked (P-3.1 title check or P-3.3 pattern)."""
    job = _gmail_job(title="Senior Frontend React Native Engineer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    # _is_react_only_title fires first (react_no_angular) before exclude_pattern reaches it
    assert reasons["react_no_angular"] + reasons["exclude_pattern"] >= 1


def test_react_native_hyphen_blocked_via_gmail() -> None:
    """react-native (hyphen variant) → exclude_pattern fires."""
    job = _gmail_job(title="Frontend React-Native Developer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["exclude_pattern"] >= 1
