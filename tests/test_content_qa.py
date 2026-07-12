"""Tests for hunter/content_qa.py — Angular skill duplicate detection."""

from __future__ import annotations

from hunter.content_qa import (
    CANONICAL_ANGULAR_SKILL,
    _check_cover_letter_en_language,
    _check_no_duplicate_angular,
    _check_no_polish_in_en_resume,
    is_angular_version_entry,
)


def test_is_angular_version_entry_true_cases() -> None:
    for item in [
        "Angular",
        "Angular (2-22)",
        "Angular 2-22",
        "Angular 2+",
        "Angular (latest versions)",
        "Angular (2-21)",
        " Angular  ",
    ]:
        assert is_angular_version_entry(item), item


def test_is_angular_version_entry_false_cases() -> None:
    for item in [
        "Angular Material",
        "Angular CLI",
        "Angular development",
        "Angular maintenance",
        "Angular Universal",
        "AngularJS",
        "TypeScript",
        "RxJS",
    ]:
        assert not is_angular_version_entry(item), item


def test_qa_passes_single_version_plus_family_skills() -> None:
    r = _check_no_duplicate_angular(
        {"skills": {"frontend": "Angular (2-22), Angular Material, Angular CLI, TypeScript"}}
    )
    assert r.passed, r.detail


def test_qa_fails_two_version_entries() -> None:
    r = _check_no_duplicate_angular(
        {"skills": {"frontend": "Angular (latest versions), Angular 2-22, TypeScript"}}
    )
    assert not r.passed
    assert "Angular" in r.detail


def test_canonical_angular_constant() -> None:
    assert CANONICAL_ANGULAR_SKILL == "Angular (2-22)"
    assert is_angular_version_entry(CANONICAL_ANGULAR_SKILL)


# ---------------------------------------------------------------------------
# Polish-contamination checks — must agree with the lang_guard enforce-gate so
# the candidate's own Polish city (Wrocław/Kraków) never trips a false QA warning.
# Regression: a clean EN cover letter mentioning "Wrocław" reported "Polish mixed
# into EN cover letter: 'ł'" because QA used a blunt diacritic regex with no
# place-name allowlist, while the gate (correctly) shipped the docs.
# ---------------------------------------------------------------------------


def test_cover_letter_en_with_polish_city_passes() -> None:
    cl = {
        "cover_letter_en": (
            "Dear Hiring Team, I am writing to apply for the Frontend Developer "
            "role. Based in Wrocław, I bring 10+ years of Angular experience and "
            "look forward to joining your team in Kraków."
        )
    }
    r = _check_cover_letter_en_language(cl)
    assert r.passed, r.detail


def test_cover_letter_en_real_polish_contamination_fails() -> None:
    cl = {
        "cover_letter_en": (
            "Dear Team, mam wieloletnie doświadczenie w tworzeniu aplikacji oraz "
            "znajomość frameworka."
        )
    }
    r = _check_cover_letter_en_language(cl)
    assert not r.passed
    assert "Polish" in r.detail


def test_resume_summary_with_polish_city_passes() -> None:
    resume_en = {
        "summary": "Senior Frontend Developer (Angular) based in Wrocław, Poland.",
        "skills": {},
        "experience": [],
    }
    assert _check_no_polish_in_en_resume(resume_en).passed


def test_resume_skill_polish_injection_fails() -> None:
    resume_en = {
        "summary": "Senior Frontend Developer",
        "skills": {"frontend": "Git / system kontroli wersji"},
        "experience": [],
    }
    r = _check_no_polish_in_en_resume(resume_en)
    assert not r.passed
    assert "skills.frontend" in r.detail
