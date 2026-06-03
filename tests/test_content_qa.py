"""Tests for hunter/content_qa.py — Angular skill duplicate detection."""

from __future__ import annotations

from hunter.content_qa import (
    CANONICAL_ANGULAR_SKILL,
    _check_no_duplicate_angular,
    is_angular_version_entry,
)


def test_is_angular_version_entry_true_cases() -> None:
    for item in ["Angular", "Angular (2-22)", "Angular 2-22", "Angular 2+",
                 "Angular (latest versions)", "Angular (2-21)", " Angular  "]:
        assert is_angular_version_entry(item), item


def test_is_angular_version_entry_false_cases() -> None:
    for item in ["Angular Material", "Angular CLI", "Angular development",
                 "Angular maintenance", "Angular Universal", "AngularJS",
                 "TypeScript", "RxJS"]:
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
