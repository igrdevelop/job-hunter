"""Tests for hunter/claim_judge.py — the LLM-as-judge CV verification pass."""

from __future__ import annotations

import llm_client
from hunter import claim_judge
from hunter.claim_judge import (
    JudgeReport,
    Violation,
    _drop_quote,
    _parse_violations,
    _resolve_path,
    iter_judged_fields,
    judge_content,
    repair_content,
)


def _content_7_roles():
    """A minimal valid content dict with 7 experience roles (validate_content passes)."""
    exp = [
        {"company": c, "period": "x", "title": "Senior Frontend Developer (Angular)",
         "bullets": [f"Did work at {c}."]}
        for c in ["Alten Poland", "Fairmarkit", "Venture Labs", "SII",
                  "Altoros", "SolbegSoft", "Staronka"]
    ]
    return {
        "company_name": "Acme", "stack": "Angular", "lang": "en",
        "job_title": "Senior Frontend Developer",
        "resume_en": {
            "summary": "Senior Angular developer with 10+ years.",
            "skills": {"frontend": "Angular, React", "languages": "English"},
            "experience": exp,
            "education": "Belarusian State Technological University",
        },
        "cover_letter_en": "Dear Hiring Manager, I am writing to apply.",
        "cover_letter_pl": "Szanowni Państwo, piszę w sprawie.",
        "about_me_en": "I build things.",
        "about_me_pl": "Buduję rzeczy.",
    }


# ---------------------------------------------------------------------------
# iter_judged_fields
# ---------------------------------------------------------------------------

def test_iter_judged_fields_covers_expected_paths():
    c = _content_7_roles()
    fields = iter_judged_fields(c)
    assert "resume_en.summary" in fields
    assert "resume_en.skills.frontend" in fields
    assert "resume_en.experience[0].bullets[0]" in fields
    assert "cover_letter_en" in fields
    assert "cover_letter_pl" in fields
    assert "about_me_en" in fields
    # languages skill is excluded (proficiency names, not claims)
    assert "resume_en.skills.languages" not in fields
    # verbatim-locked fields are excluded
    assert "resume_en.education" not in fields
    assert not any(".title" in k or ".company" in k for k in fields)


def test_iter_judged_fields_skips_empty_and_missing():
    c = {"resume_en": {"summary": "  ", "skills": {}, "experience": []}}
    assert iter_judged_fields(c) == {}


def test_iter_judged_fields_skills_list_joined():
    c = {"resume_en": {"skills": {"frontend": ["Angular", "React"]}, "experience": []}}
    fields = iter_judged_fields(c)
    assert fields["resume_en.skills.frontend"] == "Angular, React"


# ---------------------------------------------------------------------------
# _resolve_path
# ---------------------------------------------------------------------------

def test_resolve_path_nested_and_list():
    c = _content_7_roles()
    holder, key = _resolve_path(c, "resume_en.summary")
    assert holder[key] == "Senior Angular developer with 10+ years."
    holder, key = _resolve_path(c, "resume_en.skills.frontend")
    assert holder[key] == "Angular, React"
    holder, key = _resolve_path(c, "resume_en.experience[3].bullets[0]")
    assert holder[key] == "Did work at SII."


def test_resolve_path_missing_returns_none():
    c = _content_7_roles()
    assert _resolve_path(c, "resume_en.nope") == (None, None)
    assert _resolve_path(c, "resume_en.experience[99].bullets[0]") == (None, None)
    assert _resolve_path(c, "garbage..path") == (None, None)


def test_resolve_path_assignment_roundtrip():
    c = _content_7_roles()
    holder, key = _resolve_path(c, "resume_en.experience[0].bullets[0]")
    holder[key] = "Rewritten."
    assert c["resume_en"]["experience"][0]["bullets"][0] == "Rewritten."


# ---------------------------------------------------------------------------
# _parse_violations (hallucination guard)
# ---------------------------------------------------------------------------

def test_parse_violations_keeps_verbatim_quote():
    fields = {"resume_en.summary": "Senior dev serving Fortune 500 clients."}
    raw = {"violations": [
        {"field": "resume_en.summary", "quote": "serving Fortune 500 clients",
         "reason": "no such client", "severity": "fabrication"},
    ]}
    vs = _parse_violations(raw, fields)
    assert len(vs) == 1
    assert vs[0].severity == "fabrication"


def test_parse_violations_drops_nonverbatim_quote():
    fields = {"resume_en.summary": "Senior dev."}
    raw = {"violations": [
        {"field": "resume_en.summary", "quote": "Fortune 500",  # not in field
         "reason": "x", "severity": "fabrication"},
    ]}
    assert _parse_violations(raw, fields) == []


def test_parse_violations_drops_unknown_field():
    fields = {"resume_en.summary": "Fortune 500 clients"}
    raw = {"violations": [
        {"field": "resume_pl.summary", "quote": "Fortune 500 clients",
         "reason": "x", "severity": "fabrication"},
    ]}
    assert _parse_violations(raw, fields) == []


def test_parse_violations_drops_bad_severity():
    fields = {"resume_en.summary": "Fortune 500 clients"}
    raw = {"violations": [
        {"field": "resume_en.summary", "quote": "Fortune 500 clients",
         "reason": "x", "severity": "made-up"},
    ]}
    assert _parse_violations(raw, fields) == []


def test_parse_violations_handles_garbage():
    assert _parse_violations({}, {}) == []
    assert _parse_violations({"violations": "nope"}, {}) == []
    assert _parse_violations({"violations": [42, None]}, {"a": "b"}) == []


# ---------------------------------------------------------------------------
# _drop_quote
# ---------------------------------------------------------------------------

def test_drop_quote_preserves_honest_clause():
    out = _drop_quote(
        "Built apps for 300+ German banks and Fortune 500 firms.",
        "and Fortune 500 firms",
    )
    assert "300+ German banks" in out
    assert "Fortune 500" not in out


def test_drop_quote_drops_leading_connector():
    out = _drop_quote(
        "Built apps for 300+ German banks and Fortune 500 firms.",
        "Fortune 500 firms",
    )
    assert "300+ German banks" in out
    assert "Fortune 500" not in out
    assert "and" not in out.split()[-1]  # no dangling "and"


def test_drop_quote_collapses_commas():
    out = _drop_quote("Angular, React, Fortune 500 experience, RxJS", "Fortune 500 experience")
    assert out == "Angular, React, RxJS"


def test_drop_quote_missing_returns_unchanged():
    assert _drop_quote("Hello world.", "not present") == "Hello world."


# ---------------------------------------------------------------------------
# judge_content
# ---------------------------------------------------------------------------

def test_judge_content_parses_findings(monkeypatch):
    monkeypatch.setattr(claim_judge, "LLM_API_KEY", "test-key")
    c = _content_7_roles()
    c["resume_en"]["summary"] = "Senior dev serving Fortune 500 clients worldwide."

    def _fake(**kwargs):
        return {"violations": [
            {"field": "resume_en.summary", "quote": "serving Fortune 500 clients",
             "reason": "no Fortune 500 in profile", "severity": "fabrication"},
        ]}
    monkeypatch.setattr(llm_client, "call_llm", _fake)

    report = judge_content(c, "some job posting")
    assert not report.passed
    assert len(report.fabrications) == 1


def test_judge_content_empty_when_no_fields():
    report = judge_content({}, "job")
    assert report.passed
    assert report.violations == []


def test_judge_content_exception_returns_passing(monkeypatch):
    monkeypatch.setattr(claim_judge, "LLM_API_KEY", "test-key")

    def _boom(**kwargs):
        raise RuntimeError("LLM down")
    monkeypatch.setattr(llm_client, "call_llm", _boom)

    report = judge_content(_content_7_roles(), "job")
    assert report.passed  # never fatal


def test_judge_content_style_only_passes(monkeypatch):
    monkeypatch.setattr(claim_judge, "LLM_API_KEY", "test-key")
    c = _content_7_roles()
    c["resume_en"]["skills"]["frontend"] = "Angular / Angular framework, React"

    def _fake(**kwargs):
        return {"violations": [
            {"field": "resume_en.skills.frontend",
             "quote": "Angular / Angular framework",
             "reason": "gloss pair", "severity": "style"},
        ]}
    monkeypatch.setattr(llm_client, "call_llm", _fake)

    report = judge_content(c, "job")
    assert report.passed  # style is not actionable
    assert len(report.violations) == 1


# ---------------------------------------------------------------------------
# repair_content
# ---------------------------------------------------------------------------

def test_repair_deterministic_drop(monkeypatch):
    c = _content_7_roles()
    c["resume_en"]["experience"][2]["bullets"][0] = (
        "Built apps for 300+ German banks and Fortune 500 firms."
    )
    report = JudgeReport(violations=[
        Violation("resume_en.experience[2].bullets[0]", "and Fortune 500 firms",
                  "no Fortune 500", "fabrication"),
    ])
    fixed, fixes = repair_content(c, report, "job")
    bullet = fixed["resume_en"]["experience"][2]["bullets"][0]
    assert "Fortune 500" not in bullet
    assert "300+ German banks" in bullet
    assert fixes


def test_repair_style_violation_not_repaired():
    c = _content_7_roles()
    report = JudgeReport(violations=[
        Violation("resume_en.skills.frontend", "Angular", "gloss", "style"),
    ])
    fixed, fixes = repair_content(c, report, "job")
    assert fixes == []
    assert fixed["resume_en"]["skills"]["frontend"] == "Angular, React"


def test_repair_rejected_when_structure_worsens(monkeypatch):
    """If a repair introduces new structural errors (e.g. a dropped role), the
    whole repair is discarded and the original content is returned unchanged."""
    c = _content_7_roles()
    c["resume_en"]["experience"][2]["bullets"][0] = (
        "Built apps for 300+ banks and Fortune 500 firms."
    )
    report = JudgeReport(violations=[
        Violation("resume_en.experience[2].bullets[0]", "and Fortune 500 firms",
                  "fabricated", "fabrication"),
    ])

    # Make validate_content report MORE errors after the repair than before.
    import hunter.apply_shared as apply_shared
    calls = {"n": 0}

    def _validate(_d):
        calls["n"] += 1
        return [] if calls["n"] == 1 else ["resume_en.experience has only 6 jobs"]
    monkeypatch.setattr(apply_shared, "validate_content", _validate)

    fixed, fixes = repair_content(c, report, "job")
    # Guard rejected the repair → original object returned, no fixes.
    assert fixed is c
    assert fixes == []
    # The original bullet is intact (deepcopy was discarded).
    assert "Fortune 500" in c["resume_en"]["experience"][2]["bullets"][0]


def test_repair_no_actionable_is_noop():
    c = _content_7_roles()
    report = JudgeReport(violations=[])
    fixed, fixes = repair_content(c, report, "job")
    assert fixed is c
    assert fixes == []


# ---------------------------------------------------------------------------
# JudgeReport
# ---------------------------------------------------------------------------

def test_report_actionable_split():
    r = JudgeReport(violations=[
        Violation("f", "q1", "r", "fabrication"),
        Violation("f", "q2", "r", "exaggeration"),
        Violation("f", "q3", "r", "style"),
    ])
    assert len(r.actionable) == 2
    assert len(r.fabrications) == 1
    assert not r.passed


def test_report_telegram_summary_clean():
    assert "clean" in JudgeReport().telegram_summary("http://x").lower()


# ---------------------------------------------------------------------------
# quote_survives + end-to-end judge→repair
# ---------------------------------------------------------------------------

def test_quote_survives():
    from hunter.claim_judge import quote_survives
    c = _content_7_roles()
    c["resume_en"]["summary"] = "Senior dev serving Fortune 500 clients."
    assert quote_survives(c, "resume_en.summary", "Fortune 500")
    assert not quote_survives(c, "resume_en.summary", "Google")
    assert not quote_survives(c, "resume_en.nope", "x")


def test_judge_then_repair_end_to_end(monkeypatch):
    """Mocked judge flags a fabricated Fortune 500 clause; repair removes it and
    keeps the honest '300+ German banks' clause."""
    monkeypatch.setattr(claim_judge, "LLM_API_KEY", "test-key")
    c = _content_7_roles()
    c["resume_en"]["experience"][2]["bullets"][0] = (
        "Built apps for 300+ German banks and Fortune 500 firms."
    )

    def _fake(**kwargs):
        return {"violations": [
            {"field": "resume_en.experience[2].bullets[0]",
             "quote": "and Fortune 500 firms",
             "reason": "no Fortune 500 client in profile",
             "severity": "fabrication"},
        ]}
    monkeypatch.setattr(llm_client, "call_llm", _fake)

    report = judge_content(c, "job posting text")
    assert not report.passed
    fixed, fixes = repair_content(c, report, "job posting text")
    bullet = fixed["resume_en"]["experience"][2]["bullets"][0]
    assert "Fortune 500" not in bullet
    assert "300+ German banks" in bullet
    # Fabrication no longer survives.
    from hunter.claim_judge import quote_survives
    assert not quote_survives(fixed, "resume_en.experience[2].bullets[0]", "and Fortune 500 firms")


def test_config_defaults():
    from hunter import config
    assert config.JUDGE_ENABLED is True
    assert config.JUDGE_MODE in ("report", "warn", "block")
    assert config.JUDGE_MAX_REPAIR_ROUNDS >= 1
    assert config.JUDGE_MODEL
