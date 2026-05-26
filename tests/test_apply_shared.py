"""Tests for hunter/apply_shared.py — shared helpers for apply pipelines."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hunter.apply_shared import (
    APPLY_MANUAL_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    ApplyError,
    _already_processed,
    _body_banlist_hits,
    _count_body_paragraphs,
    _count_metrics,
    _count_words,
    _cta_banlist_hits,
    _last_paragraph_text,
    _opener_banlist_hits,
    _sanitize_folder_company,
    compute_output_folder,
    validate_content,
)


# ── Constants ─────────────────────────────────────────────────────────────────

def test_apply_manual_exit_code_is_44() -> None:
    assert APPLY_MANUAL_EXIT_CODE == 44


def test_paste_no_url_placeholder_non_empty() -> None:
    assert PASTE_NO_URL_PLACEHOLDER
    assert "://" in PASTE_NO_URL_PLACEHOLDER


def test_apply_error_is_runtime_error() -> None:
    with pytest.raises(ApplyError):
        raise ApplyError("test")
    with pytest.raises(RuntimeError):
        raise ApplyError("test")


# ── _already_processed ────────────────────────────────────────────────────────

def test_already_processed_skip_dedup_returns_false() -> None:
    """skip_dedup=True should always return False (never blocked)."""
    assert _already_processed("https://example.com/job/1", skip_dedup=True) is False


def test_already_processed_paste_placeholder_returns_false() -> None:
    assert _already_processed(PASTE_NO_URL_PLACEHOLDER) is False


def test_already_processed_empty_url_returns_false() -> None:
    assert _already_processed("") is False


def test_already_processed_delegates_to_tracker_service(monkeypatch) -> None:
    monkeypatch.setattr(
        "hunter.services.tracker_service.should_skip_url",
        lambda url: True,
    )
    assert _already_processed("https://justjoin.it/job/foo") is True


def test_already_processed_returns_false_on_tracker_exception(monkeypatch) -> None:
    def _boom(url: str) -> bool:
        raise RuntimeError("tracker locked")

    monkeypatch.setattr("hunter.services.tracker_service.should_skip_url", _boom)
    assert _already_processed("https://justjoin.it/job/foo") is False


def test_already_processed_does_not_mutate_sys_path() -> None:
    import sys
    before = list(sys.path)
    _already_processed("https://example.com/jobs/42")
    assert list(sys.path) == before


# ── _sanitize_folder_company ──────────────────────────────────────────────────

def test_sanitize_strips_illegal_chars() -> None:
    assert "/" not in _sanitize_folder_company("Acme/Corp")
    assert "\\" not in _sanitize_folder_company("Acme\\Corp")
    assert ":" not in _sanitize_folder_company("C:Corp")


def test_sanitize_empty_returns_unknown() -> None:
    assert _sanitize_folder_company("") == "Unknown"
    assert _sanitize_folder_company("   ") == "Unknown"


def test_sanitize_truncates_to_120() -> None:
    long_name = "A" * 200
    assert len(_sanitize_folder_company(long_name)) <= 120


def test_sanitize_normal_name_unchanged() -> None:
    assert _sanitize_folder_company("Google LLC") == "Google LLC"


# ── compute_output_folder ─────────────────────────────────────────────────────

def test_compute_output_folder_returns_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", tmp_path)
    result = compute_output_folder("Acme")
    assert isinstance(result, Path)
    assert "Acme" in result.name


def test_compute_output_folder_suffix_on_collision(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", tmp_path)
    # Pre-create the base folder
    first = compute_output_folder("Acme")
    first.mkdir(parents=True, exist_ok=True)
    # Next call should produce _2 suffix
    second = compute_output_folder("Acme")
    assert second != first
    assert "Acme_2" in second.name


# ── validate_content ──────────────────────────────────────────────────────────

def _make_valid_content() -> dict:
    """Build a content dict that passes validate_content() regardless of GENERATE_PL_RESUME."""
    from hunter.apply_shared import REQUIRED_JSON_KEYS
    content: dict = {
        "company_name": "Acme",
        "stack": "Angular, TypeScript",
        "lang": "en",
        "job_title": "Frontend Developer",
        "resume_en": {
            "summary": "Senior FE dev",
            "skills": ["Angular"],
            "experience": [
                {"company": "A"}, {"company": "B"}, {"company": "C"},
                {"company": "D"}, {"company": "E"}, {"company": "F"},
            ],
            "education": [{"degree": "BSc"}],
        },
        "resume_pl": {"summary": "Senior FE", "skills": [], "experience": [], "education": []},
        "cover_letter_en": "Dear Hiring Manager, ...",
        "cover_letter_pl": "Szanowny Panie ...",
        "about_me_en": "I am a senior Angular developer.",
        "about_me_pl": "Jestem seniorem frontend.",
    }
    # Ensure all required keys are present even if config changes
    for key in REQUIRED_JSON_KEYS:
        if key not in content:
            content[key] = "placeholder"
    return content


def test_validate_content_valid() -> None:
    content = _make_valid_content()
    errors = validate_content(content)
    assert errors == []


def test_validate_content_missing_field() -> None:
    content = _make_valid_content()
    del content["cover_letter_en"]
    errors = validate_content(content)
    assert any("cover_letter_en" in e for e in errors)


def test_validate_content_resume_not_dict() -> None:
    content = _make_valid_content()
    content["resume_en"] = "plain string"
    errors = validate_content(content)
    assert any("resume_en is not a dict" in e for e in errors)


def test_validate_content_resume_missing_subfield() -> None:
    content = _make_valid_content()
    del content["resume_en"]["skills"]
    errors = validate_content(content)
    assert any("skills" in e for e in errors)


def test_validate_content_experience_too_short() -> None:
    content = _make_valid_content()
    content["resume_en"]["experience"] = [{"company": "A"}, {"company": "B"}]
    errors = validate_content(content)
    assert any("experience" in e for e in errors)


# ── Cover letter review helpers ───────────────────────────────────────────────

def test_count_words_empty() -> None:
    assert _count_words("") == 0


def test_count_words_sentence() -> None:
    assert _count_words("hello world foo") == 3


def test_count_metrics_percentages() -> None:
    assert _count_metrics("Improved performance by 40% across 3 teams") >= 2


def test_count_metrics_excludes_10_years() -> None:
    # "10+ years" should be excluded per the regex
    count_with = _count_metrics("10+ years of experience")
    count_without = _count_metrics("five years of experience")
    assert count_with == count_without


def test_last_paragraph_text_blank_line_split() -> None:
    letter = "First paragraph.\n\nSecond paragraph."
    assert _last_paragraph_text(letter) == "Second paragraph."


def test_last_paragraph_text_empty() -> None:
    assert _last_paragraph_text("") == ""


def test_count_body_paragraphs_with_salutation() -> None:
    letter = "Dear Hiring Manager,\n\nFirst body.\n\nSecond body.\n\nThird body."
    assert _count_body_paragraphs(letter) == 3


def test_count_body_paragraphs_no_salutation() -> None:
    letter = "First.\n\nSecond.\n\nThird."
    assert _count_body_paragraphs(letter) == 3


def test_cta_banlist_hits_banned_phrase() -> None:
    letter = "Best regards.\n\nI look forward to hearing from you."
    hits = _cta_banlist_hits(letter)
    assert hits


def test_cta_banlist_allowed_cta() -> None:
    letter = "Best regards.\n\nI look forward to discussing the role."
    hits = _cta_banlist_hits(letter)
    assert hits == []


# ── notify (smoke — no real Telegram call) ───────────────────────────────────

def test_notify_no_credentials_is_silent(monkeypatch) -> None:
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr("hunter.apply_shared.TELEGRAM_CHAT_ID", "")
    # Should not raise
    from hunter.apply_shared import notify
    notify("test message")


# ── Backward compat re-exports from apply_agent ───────────────────────────────

def test_apply_agent_reexports_already_processed() -> None:
    import apply_agent
    assert hasattr(apply_agent, "_already_processed")
    assert callable(apply_agent._already_processed)


def test_apply_agent_reexports_banlist_functions() -> None:
    import apply_agent
    assert hasattr(apply_agent, "_body_banlist_hits")
    assert hasattr(apply_agent, "_opener_banlist_hits")


def test_apply_agent_reexports_constants() -> None:
    import apply_agent
    assert apply_agent.APPLY_MANUAL_EXIT_CODE == 44
    assert apply_agent.PASTE_NO_URL_PLACEHOLDER == PASTE_NO_URL_PLACEHOLDER
