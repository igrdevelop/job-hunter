"""Tests for hunter.apply_shared.enforce_language_separation (language enforce-gate)."""

import json

import pytest

import llm_client
from hunter import apply_shared


# A clean, fully-English resume with 7 roles (matches the expected role count).
_CLEAN_EN_RESUME = {
    "summary": "Senior Frontend Developer with 10+ years of Angular and TypeScript expertise.",
    "skills": {
        "frontend": "Angular, TypeScript, RxJS, responsive interfaces",
        "languages": "English (Fluent), Polish (B2)",
    },
    "experience": [
        {"company": c, "stack_line": "Stack: Angular, TypeScript",
         "bullets": ["Built scalable applications for in-house projects"]}
        for c in ("Alten Poland", "Fairmarkit", "Venture Labs", "SII",
                  "Altoros", "SolbegSoft", "Staronka")
    ],
}

_CLEAN_PL_RESUME = {
    "summary": "Senior Frontend Developer z 10-letnim doświadczeniem w Angular i TypeScript.",
    "skills": {
        "frontend": "Angular, TypeScript, RxJS, responsywne interfejsy",
        "languages": "Angielski (Płynny), Polski (B2)",
    },
    "experience": [
        {"company": c, "stack_line": "Stack: Angular, TypeScript",
         "bullets": ["Zbudowałem skalowalne aplikacje na projekty wewnętrzne"]}
        for c in ("Alten Poland", "Fairmarkit", "Venture Labs", "SII",
                  "Altoros", "SolbegSoft", "Staronka")
    ],
}


@pytest.fixture
def with_api_key(monkeypatch):
    monkeypatch.setattr(apply_shared, "LLM_API_KEY", "test-key")


def test_clean_content_no_llm_call(monkeypatch, with_api_key):
    """No contamination → gate returns unchanged, no LLM call, not blocked."""
    called = {"n": 0}

    def _fake(*a, **k):
        called["n"] += 1
        return {}

    monkeypatch.setattr(llm_client, "call_llm", _fake)
    content = {"resume_en": _CLEAN_EN_RESUME, "cover_letter_en": "Dear Hiring Manager, I apply."}
    out, blocked, report = apply_shared.enforce_language_separation(content, "EN")
    assert blocked is False
    assert report == []
    assert called["n"] == 0


def test_contaminated_en_retranslated_from_clean_pl(monkeypatch, with_api_key):
    """PL posting: contaminated resume_en is rebuilt from the clean resume_pl."""

    def _fake(system_prompt, user_message, **k):
        # The gate asks to translate the (clean) PL resume into English → return clean EN.
        return {"resume": json.loads(json.dumps(_CLEAN_EN_RESUME))}

    monkeypatch.setattr(llm_client, "call_llm", _fake)

    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["summary"] = "Senior Frontend Developer (7+ lat doświadczenia)."
    contaminated_en["skills"]["frontend"] = (
        "Angular, responsywne interfejsy (responsive interfaces)"
    )
    content = {"resume_en": contaminated_en, "resume_pl": _CLEAN_PL_RESUME}

    out, blocked, report = apply_shared.enforce_language_separation(content, "PL")
    assert blocked is False
    assert "doświadczenia" not in json.dumps(out["resume_en"], ensure_ascii=False)
    assert any("re-translated from clean resume_pl" in r for r in report)


def test_block_when_contamination_survives(monkeypatch, with_api_key):
    """If the repair pass still returns Polish, the gate blocks delivery."""

    def _fake(system_prompt, user_message, **k):
        # Repair fails to clean — returns a still-contaminated resume.
        bad = json.loads(json.dumps(_CLEAN_EN_RESUME))
        bad["summary"] = "Senior Developer z dużym doświadczeniem w Angular."
        return {"resume": bad}

    monkeypatch.setattr(llm_client, "call_llm", _fake)

    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["summary"] = "Senior Developer (7+ lat doświadczenia)."
    content = {"resume_en": contaminated_en, "resume_pl": _CLEAN_PL_RESUME}

    out, blocked, report = apply_shared.enforce_language_separation(content, "PL")
    assert blocked is True
    assert any("BLOCKED" in r for r in report)


def test_role_drop_rejected(monkeypatch, with_api_key):
    """A translation that drops experience entries is rejected (None)."""

    def _fake(system_prompt, user_message, **k):
        short = json.loads(json.dumps(_CLEAN_EN_RESUME))
        short["experience"] = short["experience"][:3]  # dropped 4 roles
        return {"resume": short}

    monkeypatch.setattr(llm_client, "call_llm", _fake)

    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["summary"] = "Senior Developer (7+ lat doświadczenia)."
    content = {"resume_en": contaminated_en, "resume_pl": _CLEAN_PL_RESUME}

    # Translation rejected → resume_en unchanged → strong Polish survives → blocked.
    out, blocked, report = apply_shared.enforce_language_separation(content, "PL")
    assert len(out["resume_en"]["experience"]) == 7  # never shipped a 3-role resume
    assert blocked is True


def test_no_api_key_no_repair(monkeypatch):
    """Without an API key the gate cannot translate; it reports but does not crash."""
    monkeypatch.setattr(apply_shared, "LLM_API_KEY", "")
    contaminated_en = json.loads(json.dumps(_CLEAN_EN_RESUME))
    contaminated_en["summary"] = "Senior Developer (7+ lat doświadczenia)."
    content = {"resume_en": contaminated_en, "resume_pl": _CLEAN_PL_RESUME}
    out, blocked, report = apply_shared.enforce_language_separation(content, "PL")
    # Strong contamination remains and could not be repaired → blocked.
    assert blocked is True
