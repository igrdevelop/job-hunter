"""hunter/outreach.py — outreach.md draft after each successful apply (#138)."""

import json
from pathlib import Path
from unittest.mock import patch

from hunter.outreach import (
    MESSAGE_CHAR_LIMIT,
    OUTREACH_FILENAME,
    _clean,
    run_outreach,
)

PL_POSTING = (
    "Poszukujemy Senior Angular Developera do zespołu w Warszawie (zdalnie).\n"
    "Wymagania: Angular 17, RxJS, NgRx, TypeScript.\n"
    "Kontakt: Anna Kowalska\n"
    "anna.kowalska@antal.pl\n"
) * 3

EN_POSTING = (
    "We are hiring a Senior Angular Developer (remote, EU).\n"
    "Requirements: Angular 17, RxJS, NgRx, TypeScript. Great benefits.\n"
) * 5


def _make_folder(tmp_path: Path, *, job_text: str = EN_POSTING,
                 primary_lang: str = "EN", with_content: bool = True) -> Path:
    folder = tmp_path / "2026-07-10" / "Acme"
    folder.mkdir(parents=True)
    (folder / "job_posting.txt").write_text(job_text, encoding="utf-8")
    if with_content:
        content = {
            "company_name": "Acme",
            "job_title": "Senior Angular Developer",
            "stack": "Angular",
            "primary_lang": primary_lang,
            "resume_en": {
                "summary": "Senior Frontend Developer with 10+ years in Angular.",
                "skills": ["Angular", "TypeScript", "RxJS"],
            },
        }
        (folder / "content.json").write_text(
            json.dumps(content, ensure_ascii=False), encoding="utf-8"
        )
    return folder


def _llm_ok(**messages):
    """Patch the drafting LLM call with a canned response."""
    resp = {"message": messages.get("message", "Short hook message."),
            "message_en": messages.get("message_en")}
    return patch("llm_client.call_llm", return_value=resp)


# ── Happy path ────────────────────────────────────────────────────────────────

def test_writes_outreach_md_with_contact_and_message(tmp_path) -> None:
    folder = _make_folder(tmp_path, job_text=PL_POSTING, primary_lang="PL")
    with _llm_ok(message="Cześć, aplikowałem na rolę Angular.",
                 message_en="Hi, I applied for the Angular role."):
        out = run_outreach(folder, "https://nofluffjobs.com/job/x")
    assert out == folder / OUTREACH_FILENAME
    text = out.read_text(encoding="utf-8")
    assert "Anna Kowalska" in text
    assert "anna.kowalska@antal.pl" in text
    assert "Cześć, aplikowałem" in text
    assert "Hi, I applied" in text            # EN version for a PL posting
    assert "https://nofluffjobs.com/job/x" in text
    assert "never sends" in text


def test_en_posting_has_single_message(tmp_path) -> None:
    folder = _make_folder(tmp_path)
    with _llm_ok(message="Hi, 10+ years of Angular..."):
        out = run_outreach(folder, "https://example.com/j")
    text = out.read_text(encoding="utf-8")
    assert text.count("## Message") == 1


def test_paste_url_not_rendered(tmp_path) -> None:
    folder = _make_folder(tmp_path)
    with _llm_ok():
        out = run_outreach(folder, "paste://no-url")
    assert "paste://" not in out.read_text(encoding="utf-8")


def test_no_contact_still_writes_message_with_hint(tmp_path) -> None:
    folder = _make_folder(tmp_path, job_text=EN_POSTING)
    with _llm_ok():
        out = run_outreach(folder, "")
    text = out.read_text(encoding="utf-8")
    assert "No contact found" in text
    assert "Short hook message." in text


# ── Degradation ───────────────────────────────────────────────────────────────

def test_llm_failure_still_writes_contact_block(tmp_path) -> None:
    folder = _make_folder(tmp_path, job_text=PL_POSTING, primary_lang="PL")
    with patch("llm_client.call_llm", side_effect=RuntimeError("api down")):
        out = run_outreach(folder, "")
    text = out.read_text(encoding="utf-8")
    assert "Anna Kowalska" in text
    assert "Draft failed" in text


def test_no_contact_and_llm_failure_writes_nothing(tmp_path) -> None:
    folder = _make_folder(tmp_path, job_text=EN_POSTING)
    with patch("llm_client.call_llm", side_effect=RuntimeError("api down")):
        assert run_outreach(folder, "") is None
    assert not (folder / OUTREACH_FILENAME).exists()


def test_missing_content_json_skips_draft_but_keeps_contact(tmp_path) -> None:
    """No content.json → no candidate summary → no LLM call, contact only."""
    folder = _make_folder(tmp_path, job_text=PL_POSTING, with_content=False)
    called = []
    with patch("llm_client.call_llm", side_effect=lambda **kw: called.append(1)):
        out = run_outreach(folder, "")
    assert called == [], "must not call the LLM without a grounded summary"
    assert "Anna Kowalska" in out.read_text(encoding="utf-8")


def test_never_raises(tmp_path) -> None:
    assert run_outreach(tmp_path / "does-not-exist", "") is None


def test_disabled_via_config(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("hunter.outreach.OUTREACH_ENABLED", False)
    folder = _make_folder(tmp_path)
    assert run_outreach(folder, "") is None
    assert not (folder / OUTREACH_FILENAME).exists()


# ── Message hygiene ───────────────────────────────────────────────────────────

def test_clean_trims_overlong_message_at_word_boundary() -> None:
    msg = _clean("word " * 100)
    assert len(msg) <= MESSAGE_CHAR_LIMIT
    assert msg.endswith("…")


def test_clean_collapses_whitespace_and_non_string() -> None:
    assert _clean("a\n\n  b\tc") == "a b c"
    assert _clean(None) == ""
    assert _clean(42) == ""


# ── Wiring (source inspection, repo convention) ───────────────────────────────

def _source_of(module: str) -> str:
    import importlib
    import inspect
    return inspect.getsource(importlib.import_module(module))


def test_api_pipeline_runs_outreach_before_success_notify() -> None:
    src = _source_of("hunter.apply_api")
    assert "from hunter.outreach import run_outreach" in src
    assert src.index("run_outreach(output_folder, url)") < src.index("Docs ready!")


def test_cli_pipeline_runs_outreach_before_success_notify() -> None:
    src = _source_of("hunter.apply_cli")
    assert "from hunter.outreach import run_outreach" in src
    assert src.index("run_outreach(folder_path, url)") < src.index("Docs ready!")
