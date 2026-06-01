"""
Tests for P-4.1 through P-9.1: config and filter patches.

P-4.1: eCommerce/CMS platform exclude_patterns
P-5.1: Node.js backend title check
P-6.1: Anti-hybrid cities in _matches_location
P-7.1: Salesforce/DevOps/SRE/mobile exclude_patterns
P-8.1: Tech Lead / Project Lead / part-time exclusions
P-9.1: German-speaking title patterns
"""
import pytest
from hunter.filters import (
    apply_filters, apply_filters_with_stats,
    _is_node_only_title, _matches_location,
    _is_german_language_required,
)
from hunter.models import Job


def _job(*, title: str, location: str = "Wroclaw", source: str = "test",
         raw: dict | None = None) -> Job:
    return Job(
        title=title, company="Acme", location=location, salary=None,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        source=source, raw=raw or {},
    )


def _gmail(*, title: str, location: str = "remote") -> Job:
    return _job(title=title, location=location, source="gmail_linkedin")


# ---------------------------------------------------------------------------
# P-4.1 — eCommerce/CMS blocked via exclude_pattern (gmail, so title_kw bypassed)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Hyva Developer",
    "Adobe Commerce Frontend",
    "PWA Studio Engineer",
    "Shopware Developer",
    "Shopify Frontend",
    "BigCommerce Developer",
    "WooCommerce Dev",
    "Drupal Developer",
    "WordPress Frontend",
    "SharePoint Developer",
    "SAP Frontend Developer",
])
def test_ecommerce_cms_blocked(title: str) -> None:
    job = _gmail(title=title)
    result, reasons = apply_filters_with_stats([job])
    assert result == [], f"{title!r} should be blocked"
    assert reasons["exclude_pattern"] >= 1, f"{title!r} — wrong reason"


# ---------------------------------------------------------------------------
# P-5.1 — Node.js backend title check
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Node.js Developer",
    "Node.js Engineer",
    "TypeScript/Node.js Developer",
    "Senior Node Developer",
])
def test_node_only_title_positive(title: str) -> None:
    assert _is_node_only_title(title), f"Expected True for: {title!r}"


@pytest.mark.parametrize("title", [
    "Frontend Node.js Developer",         # has 'frontend'
    "Angular + Node.js Full Stack",        # has 'angular'
    "React/Node.js Developer",             # has 'react'
    "UI / Node.js Developer",              # has 'ui'
])
def test_node_only_title_negative(title: str) -> None:
    assert not _is_node_only_title(title), f"Expected False for: {title!r}"


def test_node_only_blocked_from_gmail() -> None:
    job = _gmail(title="TypeScript/Node.js Developer")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["exclude_pattern"] >= 1


def test_frontend_node_passes_filter() -> None:
    """Frontend + Node.js combo should NOT be blocked."""
    job = _gmail(title="Frontend Developer (Angular + Node.js)")
    result = apply_filters([job])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# P-6.1 — Anti-hybrid cities
# ---------------------------------------------------------------------------

def test_location_wroclaw_passes() -> None:
    job = _job(title="Angular Developer", location="Wrocław (Hybrid)")
    assert _matches_location(job)


def test_location_remote_passes() -> None:
    job = _job(title="Angular Developer", location="Remote")
    assert _matches_location(job)


def test_location_krakow_rejected() -> None:
    job = _job(title="Angular Developer", location="Kraków (Hybrid)")
    assert not _matches_location(job)


def test_location_warsaw_rejected() -> None:
    job = _job(title="Angular Developer", location="Warsaw")
    assert not _matches_location(job)


def test_location_poland_with_krakow_in_title_rejected() -> None:
    """LinkedIn returns 'Poland' but city is in title — should be caught."""
    job = _job(title="Angular Dev Kraków - Zabłocie", location="Poland")
    assert not _matches_location(job)


def test_location_poland_no_city_rejected() -> None:
    """'Poland' alone (no city, no remote) must be rejected."""
    job = _job(title="Angular Developer", location="Poland")
    assert not _matches_location(job)


def test_location_wroclaw_in_title_and_poland_passes() -> None:
    """If title has Wrocław, job should pass even if location='Poland'."""
    job = _job(title="Angular Developer Wrocław", location="Poland")
    assert _matches_location(job)


# ---------------------------------------------------------------------------
# P-7.1 — Salesforce/DevOps/SRE/mobile blocked (via gmail to bypass title_kw)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Salesforce Developer",
    "DevOps Engineer",
    "SRE Engineer",
    "Platform Engineer",
    "Cloud Engineer",
    "Android Developer",
    "Flutter Developer",
    "Automation Engineer",
    "Testing Engineer",
])
def test_devops_mobile_blocked(title: str) -> None:
    job = _gmail(title=title)
    result = apply_filters([job])
    assert result == [], f"{title!r} should be blocked"


# ---------------------------------------------------------------------------
# P-8.1 — Tech Lead / Project Lead / part-time excluded
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("title", [
    "Tech Lead Angular",
    "Angular Tech Lead",
    "Project Lead Frontend",
    "Engineering Manager",
    "Frontend Developer Part-Time",
    "Angular Developer part time",
])
def test_lead_management_parttime_blocked(title: str) -> None:
    job = _gmail(title=title)
    result, reasons = apply_filters_with_stats([job])
    assert result == [], f"{title!r} should be blocked"
    # Could be level or exclude_pattern
    assert reasons["level"] + reasons["exclude_pattern"] >= 1


def test_senior_frontend_not_affected_by_lead_patterns() -> None:
    """'Senior' is not a lead/management level — should not be blocked."""
    job = _gmail(title="Senior Frontend Developer Angular")
    result = apply_filters([job])
    assert len(result) == 1


# ---------------------------------------------------------------------------
# P-9.1 — German-speaking title patterns
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw_text", [
    "Frontend Developer with German",
    "Angular Engineer (German)",
    "Frontend Developer - German Speaking",
    "Angular Dev German speaking",
])
def test_german_title_patterns(raw_text: str) -> None:
    """P-9.1 title patterns should trigger German filter via _GERMAN_REQUIRED_RES."""
    from hunter.filters import _GERMAN_REQUIRED_RES
    assert any(p.search(raw_text) for p in _GERMAN_REQUIRED_RES), (
        f"No German pattern matched: {raw_text!r}"
    )


def test_german_blocked_via_filter_with_title_signal() -> None:
    """Job with 'with German' in title must be blocked by german filter."""
    job = _gmail(title="Angular Developer with German")
    result, reasons = apply_filters_with_stats([job])
    assert result == []
    assert reasons["german"] == 1
