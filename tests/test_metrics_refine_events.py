"""Per-round refine events in pipeline_events (docs/PIPELINE_VIZ_PLAN.md M1).

`hunter.verdict_refine.refine_loop` takes an optional `run_id`; when set, it
writes one `pipeline_events` row per attempted round — stage `refine`, event
= the round's `verdict_history` outcome — plus one `start` row. The loop's
own logic is pinned by tests/test_verdict_refine.py; this module only checks
the telemetry side, with the same fakes (a canned rewrite, a scripted
sequence of verdicts, pass-through safety stages).
"""

from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

import llm_client
import hunter.apply_shared as apply_shared
import hunter.claim_judge as claim_judge
import hunter.llm_profiles as llm_profiles
import hunter.resume_sanitizer as resume_sanitizer
from hunter import ats_pdf_roundtrip, metrics
from hunter.verdict_refine import refine_loop


@pytest.fixture()
def metrics_db(tmp_path, monkeypatch):
    db = tmp_path / "metrics.db"
    monkeypatch.setattr(metrics, "DB_PATH", db)
    return db


def _events(db, run_id) -> list[dict]:
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT stage, event, payload FROM pipeline_events WHERE run_id = ? ORDER BY id",
        (run_id,),
    ).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append(
            {
                "stage": r["stage"],
                "event": r["event"],
                "payload": json.loads(r["payload"]) if r["payload"] else None,
            }
        )
    return out


def _resume(n_roles: int = 7) -> dict:
    return {
        "summary": "Senior Frontend Developer with Angular expertise.",
        "skills": {"frontend": "Angular, TypeScript"},
        "experience": [
            {"company": f"Company{i}", "bullets": ["Did things"]} for i in range(n_roles)
        ],
        "education": "BSc Computer Science",
    }


def _content() -> dict:
    return {
        "company_name": "Acme",
        "stack": "Angular",
        "lang": "EN",
        "job_title": "Senior Frontend Developer",
        "resume_en": _resume(),
        "cover_letter_en": "Dear Hiring Manager,",
        "cover_letter_pl": "Szanowni Panstwo,",
        "about_me_en": "About me EN",
        "about_me_pl": "About me PL",
        "primary_lang": "EN",
        "to_learn": "",
    }


def _v(score, missing=None) -> dict:
    return {
        "score": score,
        "missing_keywords": missing or ["Docker"],
        "recommendations": [],
        "gap_report": "",
    }


def _patch_loop(monkeypatch, verdicts: list[dict], *, blocked: bool = False) -> None:
    """Fake the rewrite LLM, the safety stages and the re-verdict — the same
    boundaries tests/test_verdict_refine.py fakes."""
    monkeypatch.setattr(
        llm_profiles,
        "get_active",
        lambda: SimpleNamespace(provider="anthropic", model="claude-test", api_key="k"),
    )
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": _resume()})
    monkeypatch.setattr(resume_sanitizer, "sanitize_content", lambda c: c)
    monkeypatch.setattr(apply_shared, "_strip_compliance_claims", lambda c: (c, []))
    monkeypatch.setattr(apply_shared, "_strip_prestige_claims", lambda c, job_text="": (c, []))
    monkeypatch.setattr(apply_shared, "_dedup_skill_glosses", lambda c: (c, []))
    monkeypatch.setattr(
        claim_judge,
        "run_judge_stage",
        lambda content, job_text, base_cv, *, enabled=True, mode="warn": SimpleNamespace(
            content=content, fixes=[]
        ),
    )
    monkeypatch.setattr(apply_shared, "enforce_language_separation", lambda c: (c, blocked, []))
    seq = iter(verdicts)
    monkeypatch.setattr(ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: next(seq))


def _run(tmp_path, *, run_id, max_rounds=2, first=70):
    return refine_loop(
        _content(),
        "job needs Docker",
        "",
        tmp_path,
        _v(first),
        regenerate_docs=lambda f: None,
        target=95,
        max_rounds=max_rounds,
        run_id=run_id,
    )


def test_refine_emits_start_then_one_event_per_round(metrics_db, tmp_path, monkeypatch):
    """Round 1 improves (accepted), round 2 does not (rejected) — the event
    log carries both decisions with the numbers the loop had in hand."""
    _patch_loop(monkeypatch, [_v(80), _v(75)])
    run_id = metrics.start_run(pipeline="api")

    out_content, out_verdict = _run(tmp_path, run_id=run_id, max_rounds=2)

    assert out_verdict["score"] == 80
    events = _events(metrics_db, run_id)
    assert [(e["stage"], e["event"]) for e in events] == [
        ("refine", "start"),
        ("refine", "accepted"),
        ("refine", "rejected"),
    ]
    assert events[0]["payload"] == {"target": 95, "max_rounds": 2, "verdict_first": 70}
    assert events[1]["payload"] == {
        "round": 1,
        "kind": "honest",
        "score": 80.0,
        "best": 80.0,
        "reason": None,
    }
    # A rejected round records the score it got AND that the best stayed put.
    assert events[2]["payload"] == {
        "round": 2,
        "kind": "honest",
        "score": 75.0,
        "best": 80.0,
        "reason": "verdict did not improve",
    }
    # The telemetry mirrors the persisted audit trail one-to-one.
    assert [h["outcome"] for h in out_content["verdict_history"]] == ["accepted", "rejected"]


def test_refine_discarded_round_is_an_event_too(metrics_db, tmp_path, monkeypatch):
    """A round the language gate blocks never reaches a verdict — it is still
    a round the page should show, with score=None."""
    _patch_loop(monkeypatch, [_v(80)], blocked=True)
    run_id = metrics.start_run(pipeline="cli")

    _run(tmp_path, run_id=run_id, max_rounds=1)

    events = _events(metrics_db, run_id)
    assert [(e["stage"], e["event"]) for e in events] == [
        ("refine", "start"),
        ("refine", "discarded"),
    ]
    assert events[1]["payload"]["score"] is None
    assert events[1]["payload"]["best"] == 70.0
    assert events[1]["payload"]["reason"] == "language gate blocked"


def test_refine_without_run_id_writes_nothing(metrics_db, tmp_path, monkeypatch):
    """The default (run_id=None) keeps every caller without telemetry —
    dual_apply's shadow, the tools — byte-for-byte unchanged: no table, no row."""
    _patch_loop(monkeypatch, [_v(80)])

    out_content, _ = _run(tmp_path, run_id=None, max_rounds=1)

    assert out_content["verdict_history"][0]["outcome"] == "accepted"
    assert not metrics_db.exists()


def test_refine_noop_run_emits_no_start(metrics_db, tmp_path, monkeypatch):
    """max_rounds=0 is the documented no-op — zero LLM calls and zero events,
    so a `start` row always means at least one round was attempted."""
    _patch_loop(monkeypatch, [])
    run_id = metrics.start_run(pipeline="api")

    _run(tmp_path, run_id=run_id, max_rounds=0)

    assert _events(metrics_db, run_id) == []
