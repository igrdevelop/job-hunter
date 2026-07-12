"""Cyrillic guard tests (M3, docs/TELEGRAM_CHANNELS_SOURCE_PLAN.md §2.7).

Covers hunter.lang_guard.cyrillic_fragments/scan_content directly, plus the
enforce_language_separation repair/block wiring in hunter.apply_shared, and a
no-false-positives pass over the existing clean EN/PL corpus fixtures already
used by test_lang_guard.py / test_lang_enforce_gate.py.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import llm_client
import hunter.llm_profiles as llm_profiles
from hunter import apply_shared
from hunter.lang_guard import (
    cyrillic_fragments,
    has_blocking_contamination,
    scan_content,
)

# ── cyrillic_fragments ────────────────────────────────────────────────────────


def test_cyrillic_fragments_extracts_ru_tokens():
    assert cyrillic_fragments("Ищем Angular-разработчика, удалёнка") == [
        "Ищем",
        "разработчика",
        "удалёнка",
    ]


def test_cyrillic_fragments_empty_on_clean_english():
    assert cyrillic_fragments("Senior Frontend Developer with Angular experience") == []


def test_cyrillic_fragments_empty_on_clean_polish():
    assert cyrillic_fragments("Starszy programista Angular, doświadczenie 7 lat") == []


def test_cyrillic_fragments_deduped_case_insensitive():
    assert cyrillic_fragments("вакансия Вакансия ВАКАНСИЯ") == ["вакансия"]


def test_cyrillic_fragments_none_input():
    assert cyrillic_fragments(None) == []
    assert cyrillic_fragments("") == []


# ── scan_content: Cyrillic folded into en_strong / pl_english ───────────────


def _content_with(resume_en_summary: str) -> dict:
    return {
        "resume_en": {
            "summary": resume_en_summary,
            "skills": {"frontend": "Angular, TypeScript"},
            "experience": [],
        }
    }


def test_scan_content_cyrillic_in_en_field_is_strong():
    content = _content_with("Ищем Senior Angular разработчика в команду.")
    scan = scan_content(content)
    assert "resume_en.summary" in scan["en_strong"]
    assert any("Ищем" in f or "разработчика" in f for f in scan["en_strong"]["resume_en.summary"])
    assert has_blocking_contamination(scan) is True


def test_scan_content_cyrillic_in_pl_field_flagged_even_alone():
    """Unlike stray anglicisms (need >=3 to flag), a single Cyrillic token
    in a _pl field is always contamination — no legitimate Cyrillic belongs
    in a Polish CV either."""
    content = {
        "resume_pl": {
            "summary": "Starszy programista Angular. Компания szuka developera.",
            "skills": {"frontend": "Angular, TypeScript"},
            "experience": [],
        }
    }
    scan = scan_content(content)
    assert "resume_pl.summary" in scan["pl_english"]
    assert any("Компания" in f for f in scan["pl_english"]["resume_pl.summary"])


def test_scan_content_clean_content_has_no_cyrillic_findings():
    content = _content_with("Senior Frontend Developer with 10+ years of Angular experience.")
    scan = scan_content(content)
    assert scan["en_strong"] == {}


# ── enforce_language_separation wiring (apply_shared) ────────────────────────

_CLEAN_EN_RESUME = {
    "summary": "Senior Frontend Developer with 10+ years of Angular and TypeScript expertise.",
    "skills": {
        "frontend": "Angular, TypeScript, RxJS, responsive interfaces",
        "languages": "English (Fluent), Polish (B2)",
    },
    "experience": [
        {
            "company": c,
            "stack_line": "Stack: Angular, TypeScript",
            "bullets": ["Built scalable applications for in-house projects"],
        }
        for c in (
            "Alten Poland",
            "Fairmarkit",
            "Venture Labs",
            "SII",
            "Altoros",
            "SolbegSoft",
            "Staronka",
        )
    ],
}

_CLEAN_PL_RESUME = {
    "summary": "Senior Frontend Developer z 10-letnim doświadczeniem w Angular i TypeScript.",
    "skills": {
        "frontend": "Angular, TypeScript, RxJS, responsywne interfejsy",
        "languages": "Angielski (Płynny), Polski (B2)",
    },
    "experience": [
        {
            "company": c,
            "stack_line": "Stack: Angular, TypeScript",
            "bullets": ["Zbudowałem skalowalne aplikacje na projekty wewnętrzne"],
        }
        for c in (
            "Alten Poland",
            "Fairmarkit",
            "Venture Labs",
            "SII",
            "Altoros",
            "SolbegSoft",
            "Staronka",
        )
    ],
}


def _fake_profile(key="test-key"):
    return SimpleNamespace(provider="anthropic", model="claude-test", api_key=key)


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile("test-key"))


def test_ru_keyword_in_resume_en_repaired_via_translate_path(monkeypatch, with_api_key):
    """A RU keyword mirrored into resume_en (ATS loop, ru posting) is repaired
    by re-translating from the clean resume_pl counterpart — same contract as
    Polish-in-English repair."""

    def _fake(system_prompt, user_message, **k):
        return {"resume": json.loads(json.dumps(_CLEAN_EN_RESUME))}

    monkeypatch.setattr(llm_client, "call_llm", _fake)

    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["skills"]["frontend"] = "Angular, TypeScript, требуется опыт"
    content = {"resume_en": contaminated_en, "resume_pl": _CLEAN_PL_RESUME}

    out, blocked, report = apply_shared.enforce_language_separation(content)
    assert blocked is False
    assert "требуется" not in json.dumps(out["resume_en"], ensure_ascii=False)
    assert any("re-translated from clean resume_pl" in r for r in report)


def test_wholly_cyrillic_field_blocks_when_repair_fails(monkeypatch, with_api_key):
    """No clean counterpart, and the repair pass still returns Cyrillic →
    delivery is blocked (same contract as surviving strong Polish)."""

    def _fake(system_prompt, user_message, **k):
        bad = json.loads(json.dumps(_CLEAN_EN_RESUME))
        bad["summary"] = "Ищем Senior Angular разработчика, удалённо."
        return {"resume": bad}

    monkeypatch.setattr(llm_client, "call_llm", _fake)

    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["summary"] = "Ищем Senior Angular разработчика, удалённо."
    content = {"resume_en": contaminated_en}  # no resume_pl to repair from

    out, blocked, report = apply_shared.enforce_language_separation(content)
    assert blocked is True
    assert any("BLOCKED" in r for r in report)


def test_clean_en_pl_corpus_has_zero_cyrillic_false_positives(with_api_key):
    """Run the existing clean EN/PL fixtures (from test_lang_enforce_gate.py)
    through the gate untouched — must produce no contamination at all, proving
    the Cyrillic guard adds no false positives on real clean content."""
    content = {"resume_en": _CLEAN_EN_RESUME, "resume_pl": _CLEAN_PL_RESUME}
    scan = scan_content(content)
    assert scan["en_strong"] == {}
    assert scan["pl_english"] == {}
