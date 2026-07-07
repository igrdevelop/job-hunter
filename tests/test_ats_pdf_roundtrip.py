"""Unit tests for hunter.ats_pdf_roundtrip.

The pipeline is best-effort: missing PDF, unreadable PDF, empty job text or
empty content should all return None rather than blowing up the apply flow.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from hunter.ats_pdf_roundtrip import (
    NBSP,
    find_en_cv_pdf,
    format_summary,
    nbsp_patch_missing_keywords,
    run_pdf_roundtrip,
)


def test_find_en_cv_pdf_prefers_cv_en_pattern(tmp_path: Path) -> None:
    (tmp_path / "Cover_Letter_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Ihar_Petrasheuski_CV_Angular_2026_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Ihar_Petrasheuski_CV_Angular_2026_PL.pdf").write_bytes(b"%PDF-1.4\n")
    found = find_en_cv_pdf(tmp_path)
    assert found is not None
    assert "CV" in found.name and "EN" in found.name and "cover" not in found.name.lower()


def test_find_en_cv_pdf_returns_none_when_only_cover_letters(tmp_path: Path) -> None:
    (tmp_path / "Cover_Letter_EN.pdf").write_bytes(b"%PDF-1.4\n")
    (tmp_path / "Cover_Letter_PL.pdf").write_bytes(b"%PDF-1.4\n")
    assert find_en_cv_pdf(tmp_path) is None


def test_run_pdf_roundtrip_empty_job_text_returns_none(tmp_path: Path) -> None:
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    assert run_pdf_roundtrip(tmp_path, job_text="   ", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_no_pdf_returns_none(tmp_path: Path) -> None:
    assert run_pdf_roundtrip(tmp_path, job_text="we need Angular", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_pdf_extract_empty_returns_none(tmp_path: Path) -> None:
    # Real pypdf will fail to extract text from a stub byte stream.
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\nnot really a pdf")
    assert run_pdf_roundtrip(tmp_path, job_text="we need Angular", json_ats_score=90.0) is None


def test_run_pdf_roundtrip_returns_score_and_delta(tmp_path: Path) -> None:
    # Bypass the real pypdf extraction so the test stays pure unit-level.
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    pdf_text = (
        "Senior Frontend Engineer Angular TypeScript RxJS NgRx React Vue.js "
        "Jest Cypress Playwright Webpack Vite Docker Kubernetes AWS GraphQL REST"
    )
    job_text = "Looking for Angular + TypeScript engineer with RxJS, NgRx, Jest experience."
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value=pdf_text):
        result = run_pdf_roundtrip(tmp_path, job_text=job_text, json_ats_score=88.0)
    assert result is not None
    assert "score" in result
    assert "delta_from_json" in result
    assert result["delta_from_json"] == round(result["score"] - 88.0, 1)
    assert result["pdf_text_chars"] == len(pdf_text)
    assert result["pdf_file"].endswith("_EN.pdf")


def test_run_pdf_roundtrip_no_json_score_skips_delta(tmp_path: Path) -> None:
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular TypeScript"):
        result = run_pdf_roundtrip(tmp_path, job_text="Angular role", json_ats_score=None)
    assert result is not None
    assert "delta_from_json" not in result


def test_format_summary_includes_delta_no_warn_flag() -> None:
    # The warn ⚠️ flag was removed: by the time format_summary runs the
    # NBSP self-heal loop has either fixed the regression or accepted it,
    # so flagging the user with an unactionable number is just noise.
    s = format_summary({"score": 92.0, "delta_from_json": -7.5})
    assert "92.0%" in s
    assert "-7.5" in s
    assert "⚠️" not in s


def test_format_summary_no_warn_when_delta_small() -> None:
    s = format_summary({"score": 96.0, "delta_from_json": -2.0})
    assert "⚠️" not in s
    assert "-2.0" in s


def test_format_summary_positive_delta_has_plus_sign() -> None:
    s = format_summary({"score": 95.0, "delta_from_json": 3.0})
    assert "+3.0" in s


def test_format_summary_without_delta() -> None:
    s = format_summary({"score": 90.0})
    assert "90.0%" in s
    assert "vs JSON" not in s


def test_nbsp_patch_replaces_internal_space_in_skills() -> None:
    # Real failure case from production: "Performance Optimization" appears in
    # skills.methodologies, ATS regex matches the JSON resume, but the PDF
    # render breaks the phrase across a line wrap and pypdf returns
    # "Performance\noptimization" — regex `\s+` matches \n but the wrapper
    # adds a stray space the substring search misses.
    content = {
        "resume_en": {
            "skills": {
                "methodologies": "Agile, TDD, Performance Optimization, CI/CD",
            },
            "experience": [
                {
                    "bullets": ["Drove performance optimization across the SPA."],
                },
            ],
        }
    }
    n = nbsp_patch_missing_keywords(content, ["Performance Optimization"])
    assert n == 2  # patched in skills AND in the bullet
    assert NBSP in content["resume_en"]["skills"]["methodologies"]
    # The original casing is preserved; only the space character changed.
    assert "Performance" + NBSP + "Optimization" in content["resume_en"]["skills"]["methodologies"]
    # Bullet also patched, case-insensitively.
    assert "performance" + NBSP + "optimization" in content["resume_en"]["experience"][0]["bullets"][0]


def test_nbsp_patch_skips_single_word_keywords() -> None:
    # Single-word missing keywords ("express", "jasmine") are NOT a render
    # artefact — they're absent from the JSON itself. NBSP can't help; the
    # earlier _ats_check_loop is responsible for rewriting content.
    content = {"resume_en": {"skills": {"tools": "Jest, Cypress, Express, Webpack"}}}
    before = content["resume_en"]["skills"]["tools"]
    n = nbsp_patch_missing_keywords(content, ["express", "webpack"])
    assert n == 0
    assert content["resume_en"]["skills"]["tools"] == before


def test_nbsp_patch_no_keywords_is_noop() -> None:
    content = {"resume_en": {"skills": {"x": "Angular"}}}
    assert nbsp_patch_missing_keywords(content, []) == 0
    assert content["resume_en"]["skills"]["x"] == "Angular"


def test_nbsp_patch_keyword_absent_from_resume_is_noop() -> None:
    # Missing keyword that doesn't appear in the resume — nothing to patch.
    content = {"resume_en": {"skills": {"x": "Angular, TypeScript"}}}
    assert nbsp_patch_missing_keywords(content, ["Continuous Integration"]) == 0


def test_nbsp_patch_handles_non_dict_resume() -> None:
    # Defensive: if resume_en is a stringified blob (rare but seen), bail out.
    content = {"resume_en": "Angular Senior"}
    assert nbsp_patch_missing_keywords(content, ["Performance Optimization"]) == 0


def test_run_pdf_roundtrip_stores_content_payload(tmp_path: Path) -> None:
    """The dict result is JSON-serializable so callers can persist it on content.json."""
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular TypeScript Jest"):
        result = run_pdf_roundtrip(tmp_path, job_text="need Angular Jest", json_ats_score=80.0)
    assert result is not None
    # Round-trips through JSON unchanged.
    assert json.loads(json.dumps(result)) == result


# ── Final independent LLM verdict (PDF) ───────────────────────────────────────

def _verdict_env(monkeypatch, *, enabled: bool = True, key: str = "test-key") -> None:
    from hunter import config
    monkeypatch.setattr(config, "ATS_VERDICT_ENABLED", enabled, raising=False)
    monkeypatch.setattr(config, "JUDGE_API_KEY", key)
    monkeypatch.setattr(config, "JUDGE_PROVIDER", "anthropic")
    monkeypatch.setattr(config, "JUDGE_MODEL", "claude-haiku-4-5-20251001")


def test_run_llm_verdict_disabled_returns_none(tmp_path: Path, monkeypatch) -> None:
    from hunter.ats_pdf_roundtrip import run_llm_verdict
    _verdict_env(monkeypatch, enabled=False)
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    assert run_llm_verdict(tmp_path, job_text="need Angular") is None


def test_run_llm_verdict_no_key_returns_none(tmp_path: Path, monkeypatch) -> None:
    from hunter.ats_pdf_roundtrip import run_llm_verdict
    _verdict_env(monkeypatch, key="")
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    assert run_llm_verdict(tmp_path, job_text="need Angular") is None


def test_run_llm_verdict_no_pdf_returns_none(tmp_path: Path, monkeypatch) -> None:
    from hunter.ats_pdf_roundtrip import run_llm_verdict
    _verdict_env(monkeypatch)
    assert run_llm_verdict(tmp_path, job_text="need Angular") is None


def test_run_llm_verdict_happy_path(tmp_path: Path, monkeypatch) -> None:
    """One LLM call over the extracted PDF text → dict with score + pdf_file."""
    from hunter.ats_pdf_roundtrip import format_verdict, run_llm_verdict
    _verdict_env(monkeypatch)
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    llm_json = {
        "ats_score": 91,
        "missing_keywords": ["GraphQL"],
        "recommendations": ["Add GraphQL"],
        "gap_report": "Minor gaps.",
    }
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular TypeScript"), \
         patch("llm_client.call_llm", return_value=llm_json) as llm_mock:
        verdict = run_llm_verdict(tmp_path, job_text="need Angular + GraphQL")
    assert verdict is not None
    assert verdict["score"] == 91.0
    assert verdict["missing_keywords"] == ["GraphQL"]
    assert verdict["model"] == "claude-haiku-4-5-20251001"
    assert verdict["pdf_file"].endswith("_EN.pdf")
    assert llm_mock.call_count == 1
    # The judge model/provider is used — never the main generation profile.
    assert llm_mock.call_args.kwargs["model"] == "claude-haiku-4-5-20251001"
    assert "91" in format_verdict(verdict)
    # JSON-serializable so callers can persist it on content.json.
    assert json.loads(json.dumps(verdict)) == verdict


def test_run_llm_verdict_llm_failure_returns_none(tmp_path: Path, monkeypatch) -> None:
    from hunter.ats_pdf_roundtrip import run_llm_verdict
    _verdict_env(monkeypatch)
    (tmp_path / "Ihar_Petrasheuski_CV_EN.pdf").write_bytes(b"%PDF-1.4\n")
    with patch("hunter.ats_pdf_roundtrip.extract_pdf_text", return_value="Angular"), \
         patch("llm_client.call_llm", side_effect=RuntimeError("boom")):
        assert run_llm_verdict(tmp_path, job_text="need Angular") is None


def test_llm_verdict_requires_inputs() -> None:
    from hunter import ats_checker
    assert ats_checker.llm_verdict("", "resume", api_key="k") is None
    assert ats_checker.llm_verdict("job", "", api_key="k") is None
    assert ats_checker.llm_verdict("job", "resume", api_key="") is None


# ── format_verdict / format_gap_report: the owner sees WHY, not just the % ───

def test_format_verdict_includes_gap_report_escaped() -> None:
    from hunter.ats_pdf_roundtrip import format_verdict
    v = {"score": 94.0, "gap_report": "Only negligible gaps: <minor> & style."}
    out = format_verdict(v)
    assert "94" in out
    assert "negligible gaps" in out
    # HTML-escaped — the bot sends notifications with parse_mode=HTML.
    assert "&lt;minor&gt; &amp; style." in out


def test_format_verdict_without_gap_is_single_line() -> None:
    from hunter.ats_pdf_roundtrip import format_verdict
    assert "\n" not in format_verdict({"score": 91.0})
    assert "\n" not in format_verdict({"score": 91.0, "gap_report": "   "})


def test_format_gap_report_truncates_long_text() -> None:
    from hunter.ats_pdf_roundtrip import format_gap_report
    out = format_gap_report({"gap_report": "x" * 1000})
    assert len(out) < 400
    assert out.endswith("…</i>")


def test_format_gap_report_empty_for_missing_gap() -> None:
    from hunter.ats_pdf_roundtrip import format_gap_report
    assert format_gap_report({}) == ""
    assert format_gap_report({"gap_report": None}) == ""
