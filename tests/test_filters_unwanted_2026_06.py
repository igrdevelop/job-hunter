"""
Tests for the 2026-06 filter hardening (tracker rows 670–767 audit):

1. Fullstack + heavy-backend block (Angular present but paired with Java/.NET/C#/
   Python in title or body); Node/Nuxt fullstack-with-Angular is kept.
2. The `\\bc#` exclude pattern actually matches "C#" (the old `\\bc#\\b` never did).
3. Body disqualifiers (Blazor/Mendix/WordPress… hidden in the description).
4. On-site/hybrid city detected in the body when the listing says "remote".
5. Cyprus cities added to the anti-hybrid set.
6. AI-training / staffing-mill exclusion (title patterns + company names).
7. New title exclude patterns: Mendix, low-code, email developer, UI designer.
"""
import pytest

from hunter.models import Job
from hunter.filters import (
    apply_filters,
    apply_filters_with_stats,
    classify_job,
    screen_job_text,
    _ANTI_HYBRID_CITIES,
    _is_unwanted_fullstack,
    _has_body_disqualifier,
    _is_unwanted_onsite_location,
    _is_ai_training_or_mill,
)


def _job(*, title: str, company: str = "Acme", location: str = "remote",
         body: str = "", source: str = "test") -> Job:
    return Job(
        title=title, company=company, location=location, salary=None,
        url=f"https://example.com/{title.lower().replace(' ', '-')}",
        source=source, raw={"description": body} if body else {},
    )


# ── 1. Fullstack + heavy backend ──────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Programista Full-Stack ze Spring Boot i Angular",   # Spring in title
    "Fullstack Developer (C# + Angular)",                # C# in title
    "Full Stack Java / Angular Developer",               # Java in title
])
def test_fullstack_backend_in_title_blocked(title: str) -> None:
    assert _is_unwanted_fullstack(_job(title=title))


def test_fullstack_backend_in_body_blocked() -> None:
    job = _job(title="FullStack Developer with Angular",
               body="You will work with Java and Spring Boot on the backend.")
    assert _is_unwanted_fullstack(job)


def test_fullstack_without_angular_always_blocked() -> None:
    assert _is_unwanted_fullstack(_job(title="Full Stack Developer"))
    assert _is_unwanted_fullstack(_job(title="Full Stack Node.js Engineer"))


def test_fullstack_angular_node_kept() -> None:
    """Angular + Node/Nuxt fullstack is intentionally allowed (owner's choice)."""
    job = _job(title="FullStack Developer with Angular",
               body="Frontend in Angular, backend in Node.js / NestJS.")
    assert not _is_unwanted_fullstack(job)


def test_plain_angular_not_fullstack_kept() -> None:
    assert not _is_unwanted_fullstack(_job(title="Senior Angular Developer"))


# ── 2. C# regex actually matches now ──────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Frontend Developer C#",
    "Frontend (C#/Angular) Engineer",
    "Frontend C# Developer",
])
def test_csharp_title_blocked(title: str) -> None:
    # via gmail-style source so title_kw passes and exclude_pattern is reached
    job = _job(title=title, source="gmail_linkedin")
    result, reasons = apply_filters_with_stats([job])
    assert result == [], f"{title!r} should be blocked by c# pattern"


# ── 3. Body disqualifiers ─────────────────────────────────────────────────────

@pytest.mark.parametrize("body", [
    "Experience with .NET and Blazor is required.",
    "We build apps in Mendix low-code platform.",
    "Maintain our WordPress sites.",
    "Drupal CMS experience needed.",
    "Knowledge of SharePoint development.",
])
def test_body_disqualifier_blocks(body: str) -> None:
    job = _job(title="Frontend Developer", body=body)
    assert _has_body_disqualifier(job)


def test_clean_body_not_blocked() -> None:
    job = _job(title="Frontend Developer",
               body="Angular, TypeScript, RxJS, NgRx. Fully remote.")
    assert not _has_body_disqualifier(job)


# ── 4. On-site / hybrid city in body ──────────────────────────────────────────

def test_body_hybrid_city_blocks_despite_remote_listing() -> None:
    job = _job(title="Angular Developer", location="Remote",
               body="This is a Hybrid position, 3 days a week in our Warsaw office.")
    assert _is_unwanted_onsite_location(job)


def test_body_cyprus_hybrid_blocks() -> None:
    job = _job(title="Frontend Developer (Angular)", location="Remote",
               body="Hybrid role based in our Limassol office.")
    assert _is_unwanted_onsite_location(job)


def test_fully_remote_body_not_blocked() -> None:
    job = _job(title="Angular Developer", location="Remote",
               body="Fully remote. Our HQ is in Kraków but you work from anywhere.")
    assert not _is_unwanted_onsite_location(job)


def test_wroclaw_hybrid_not_blocked() -> None:
    job = _job(title="Angular Developer", location="Wrocław (Hybrid)",
               body="Hybrid, 2 days a week in our Wrocław office.")
    assert not _is_unwanted_onsite_location(job)


def test_city_mention_without_onsite_signal_not_blocked() -> None:
    job = _job(title="Angular Developer", location="Remote",
               body="We are a Kraków-founded startup. Team is fully distributed.")
    assert not _is_unwanted_onsite_location(job)


# ── 4b. Weekly-hybrid exception (Warsaw / Kraków, ~1 day a week) ───────────────

@pytest.mark.parametrize("location,body", [
    ("Remote", "Hybrid, 1 day a week in our Warsaw office."),
    ("Kraków (Hybrid)", "Hybrid model: once a week in the office."),
    ("Kraków", "Praca hybrydowa, raz w tygodniu w biurze."),
    ("Remote", "One day a week in the Warsaw office, rest remote."),
])
def test_weekly_hybrid_warsaw_krakow_kept(location: str, body: str) -> None:
    job = _job(title="Angular Developer", location=location, body=body)
    assert classify_job(job) is None, f"{location!r}/{body!r} should be kept"


@pytest.mark.parametrize("location,body", [
    ("Remote", "Hybrid, 3 days a week in our Warsaw office."),
    ("Kraków (Hybrid)", "Hybrid work model based in Kraków."),          # no frequency
    ("Remote", "Hybrid, 1 day a week in our Limassol office."),         # far city
    ("Remote", "1 day a week in Warsaw; we also have a Berlin office."),  # other city too
])
def test_non_weekly_or_far_hybrid_rejected(location: str, body: str) -> None:
    job = _job(title="Angular Developer", location=location, body=body)
    assert classify_job(job) == "location", f"{location!r}/{body!r} should be rejected"


def test_weekly_hybrid_disabled_by_config(monkeypatch) -> None:
    from hunter import filters
    patched = {**filters.FILTER, "allow_weekly_hybrid_warsaw_krakow": False}
    monkeypatch.setattr(filters, "FILTER", patched)
    job = _job(title="Angular Developer", location="Remote",
               body="Hybrid, 1 day a week in our Warsaw office.")
    assert filters.classify_job(job) == "location"


# ── 5. Cyprus cities in anti-hybrid set ───────────────────────────────────────

@pytest.mark.parametrize("city", ["limassol", "nicosia", "larnaca", "paphos"])
def test_cyprus_cities_in_set(city: str) -> None:
    assert city in _ANTI_HYBRID_CITIES


# ── 6. AI training / staffing mills ───────────────────────────────────────────

@pytest.mark.parametrize("company", [
    "QuikHireStaffing", "HireFeed", "micro1", "Alignerr", "Mercor",
])
def test_mill_company_blocked(company: str) -> None:
    assert _is_ai_training_or_mill(_job(title="Angular Frontend Developer", company=company))


def test_normal_company_not_blocked() -> None:
    assert not _is_ai_training_or_mill(_job(title="Angular Developer", company="Allegro"))


def test_ai_training_title_blocked() -> None:
    job = _job(title="Frontend TypeScript Engineer (AI Training)", source="gmail_linkedin")
    result = apply_filters([job])
    assert result == []


# ── 7. New title exclude patterns ─────────────────────────────────────────────

@pytest.mark.parametrize("title", [
    "Mendix Frontend Developer",
    "Frontend Low-Code Developer",
    "Front-End Solution Consultant / Email Developer",
    "Frontend Engineer & UI Designer",
])
def test_new_title_patterns_blocked(title: str) -> None:
    job = _job(title=title, source="gmail_linkedin")
    result = apply_filters([job])
    assert result == [], f"{title!r} should be blocked"


# ── End-to-end: a clean Angular role still passes ─────────────────────────────

def test_clean_angular_role_passes() -> None:
    job = _job(title="Senior Angular Developer", location="Remote",
               body="Angular 17, TypeScript, RxJS, NgRx. Fully remote within the EU.")
    assert classify_job(job) is None
    assert apply_filters([job]) == [job]


# ── screen_job_text (manual "warn but allow" path) ────────────────────────────

def test_screen_flags_body_disqualifier() -> None:
    reason = screen_job_text("We build everything in Mendix low-code.")
    assert reason is not None
    assert "tech" in reason or "platform" in reason


def test_screen_flags_hybrid_city() -> None:
    reason = screen_job_text(
        "Hybrid role, 3 days a week in our Warsaw office.")
    assert reason is not None
    assert "Wroc" in reason or "on-site" in reason or "hybrid" in reason.lower()


def test_screen_flags_fullstack_backend_with_title() -> None:
    reason = screen_job_text(
        "Backend in Java and Spring Boot.",
        title="Full Stack Developer with Angular")
    assert reason is not None


def test_screen_flags_mill_company() -> None:
    reason = screen_job_text("Angular role.", company="QuikHireStaffing")
    assert reason is not None


def test_screen_clean_posting_returns_none() -> None:
    reason = screen_job_text(
        "Senior Angular Developer. Angular 17, TypeScript, NgRx. Fully remote.",
        title="Senior Angular Developer")
    assert reason is None


def test_screen_does_not_enforce_title_keyword() -> None:
    """Manual override: a non-FE title alone must NOT trigger a warning."""
    reason = screen_job_text(
        "Great backend-free role building dashboards.",
        title="Senior Software Engineer")
    assert reason is None
