"""Tests for hunter.verdict_refine (docs/VERDICT_REFINE_PLAN.md).

Mocking strategy: the refine loop re-runs the pipeline's own safety stages
(sanitize/scrubs/judge/language-gate) each round — those are exercised by
their own test modules, so here they're patched to pass-through (or to
signal `blocked` for the one test that needs it) and the focus stays on the
loop's orchestration: accept/rollback, escalation, to_learn, and the
deterministic feedback filter.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import llm_client
import hunter.apply_shared as apply_shared
import hunter.claim_judge as claim_judge
import hunter.llm_profiles as llm_profiles
import hunter.resume_sanitizer as resume_sanitizer
from hunter import ats_pdf_roundtrip, verdict_refine
from hunter.verdict_refine import build_refine_feedback, refine_loop


def _fake_profile(key: str = "test-key") -> SimpleNamespace:
    return SimpleNamespace(provider="anthropic", model="claude-test", api_key=key)


def _resume(n_roles: int = 7) -> dict:
    return {
        "summary": "Senior Frontend Developer with Angular expertise.",
        "skills": {"frontend": "Angular, TypeScript"},
        "experience": [
            {"company": f"Company{i}", "bullets": ["Did things"]} for i in range(n_roles)
        ],
        "education": "BSc Computer Science",
    }


def _base_content(**overrides) -> dict:
    content = {
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
    content.update(overrides)
    return content


def _patch_safety_stages(monkeypatch, *, blocked: bool = False) -> None:
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


def _v(score, missing=None, recs=None, gap="") -> dict:
    return {
        "score": score,
        "missing_keywords": missing or [],
        "recommendations": recs or [],
        "gap_report": gap,
    }


# ── build_refine_feedback ─────────────────────────────────────────────────────

def test_build_refine_feedback_drops_unfixable_items():
    verdict = _v(
        80,
        missing=["Docker", "on-site presence"],
        recs=[
            "Add a cover note",
            "Update your LinkedIn profile",
            "Consider relocating closer to the office",
            "Mention Docker explicitly in skills",
        ],
        gap="Minor gaps.",
    )
    feedback = build_refine_feedback(verdict)
    assert feedback is not None
    assert "Docker" in feedback
    assert "Mention Docker" in feedback
    assert "cover note" not in feedback.lower()
    assert "linkedin" not in feedback.lower()
    assert "relocat" not in feedback.lower()
    assert "on-site" not in feedback.lower()


def test_build_refine_feedback_returns_none_when_nothing_actionable():
    verdict = _v(
        80,
        missing=["on-site presence"],
        recs=[
            "Add a cover note",
            "Update your LinkedIn profile",
            "Consider relocating",
        ],
        gap="Location mismatch.",
    )
    assert build_refine_feedback(verdict) is None


def test_build_refine_feedback_none_verdict_is_safe():
    assert build_refine_feedback({}) is None
    assert build_refine_feedback(None) is None  # type: ignore[arg-type]


# ── refine_loop: no-op paths (0 LLM calls) ────────────────────────────────────

def test_refine_loop_noop_when_max_rounds_zero(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("call_llm must not be called when max_rounds=0")

    monkeypatch.setattr(llm_client, "call_llm", _boom)
    content = _base_content()
    verdict = _v(50, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job text", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=0,
    )
    assert out_content is content
    assert out_verdict is verdict
    assert not (tmp_path / "content.json").exists()


def test_refine_loop_noop_when_already_at_target(tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("call_llm must not be called when verdict already at target")

    monkeypatch.setattr(llm_client, "call_llm", _boom)
    content = _base_content()
    verdict = _v(96, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job text", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=2,
    )
    assert out_content is content
    assert out_verdict is verdict


# ── refine_loop: accept / rollback ────────────────────────────────────────────

def test_refine_loop_accepts_when_verdict_improves(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    revised = _resume()
    revised["skills"]["frontend"] += ", Docker"
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": revised})

    new_verdict = _v(90)
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: new_verdict
    )

    regen_calls = []
    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job needs Docker", "", tmp_path, verdict,
        regenerate_docs=lambda f: regen_calls.append(f), target=95, max_rounds=1,
    )

    assert out_verdict == new_verdict
    assert out_content["resume_en"]["skills"]["frontend"].endswith("Docker")
    assert len(regen_calls) == 1
    saved = json.loads((tmp_path / "content.json").read_text(encoding="utf-8"))
    assert saved["resume_en"]["skills"]["frontend"].endswith("Docker")


def test_refine_loop_rolls_back_when_verdict_does_not_improve(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    def _fake_llm(system_prompt, user_message, **k):
        bad = _resume()
        bad["summary"] = "A worse rewrite"
        return {"resume_en": bad}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    worse_verdict = _v(75)
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: worse_verdict
    )

    regen_calls = []
    content = _base_content()
    original_summary = content["resume_en"]["summary"]
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: regen_calls.append(f), target=95, max_rounds=1,
    )

    assert out_verdict == verdict  # old verdict kept — no regression
    assert out_content["resume_en"]["summary"] == original_summary
    # Round render + rollback render = 2 regen calls.
    assert len(regen_calls) == 2
    saved = json.loads((tmp_path / "content.json").read_text(encoding="utf-8"))
    assert saved["resume_en"]["summary"] == original_summary


def test_refine_loop_discards_round_on_language_block(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch, blocked=True)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": _resume()})

    def _boom(*a, **k):
        raise AssertionError("must not re-verdict a blocked round")

    monkeypatch.setattr(ats_pdf_roundtrip, "run_llm_verdict", _boom)

    regen_calls = []
    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: regen_calls.append(f), target=95, max_rounds=1,
    )

    assert out_content == content
    assert out_verdict == verdict
    assert regen_calls == []
    assert not (tmp_path / "content.json").exists()


def test_refine_loop_exception_in_rewrite_returns_original(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    def _boom(*a, **k):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(llm_client, "call_llm", _boom)
    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=1,
    )
    assert out_content is content
    assert out_verdict is verdict
    assert not (tmp_path / "content.json").exists()


# ── refine_loop: PL mirroring ─────────────────────────────────────────────────

def test_refine_loop_mirrors_to_pl_once_after_accepted_round(tmp_path, monkeypatch):
    """PL mirroring happens ONCE, after the loop, and only for an accepted
    round — not per round (Fix 4: a translate call on a rolled-back round is
    wasted money)."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": _resume()})
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(90)
    )

    translate_calls = []

    def _fake_translate(resume, target_lang, *, expected_roles):
        translate_calls.append(target_lang)
        return _resume()

    monkeypatch.setattr(apply_shared, "_translate_resume", _fake_translate)

    regen_calls = []
    content = _base_content(primary_lang="PL")
    content["resume_pl"] = _resume()
    verdict = _v(80, missing=["Docker"])
    out_content, _ = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: regen_calls.append(f), target=95, max_rounds=1,
    )
    assert translate_calls == ["PL"]
    assert "resume_pl" in out_content
    # Round render + final mirror render = 2 regen calls.
    assert len(regen_calls) == 2
    saved = json.loads((tmp_path / "content.json").read_text(encoding="utf-8"))
    assert saved["resume_pl"] == out_content["resume_pl"]


def test_refine_loop_does_not_mirror_when_primary_lang_en(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": _resume()})
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(90)
    )

    def _boom(*a, **k):
        raise AssertionError("must not translate for an EN-primary posting")

    monkeypatch.setattr(apply_shared, "_translate_resume", _boom)

    content = _base_content(primary_lang="EN")
    verdict = _v(80, missing=["Docker"])
    refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=1,
    )


def test_refine_loop_does_not_mirror_pl_on_full_rollback(tmp_path, monkeypatch):
    """A PL posting whose only round gets rolled back (verdict didn't
    improve) must not spend a translate call — no round was ever accepted."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    monkeypatch.setattr(llm_client, "call_llm", lambda *a, **k: {"resume_en": _resume()})
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(75)
    )

    def _boom(*a, **k):
        raise AssertionError("must not translate when every round was rolled back")

    monkeypatch.setattr(apply_shared, "_translate_resume", _boom)

    content = _base_content(primary_lang="PL")
    content["resume_pl"] = _resume()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=1,
    )
    assert out_content is content
    assert out_verdict is verdict


# ── refine_loop: escalation (rounds 1-2 honest, round 3 stretch) ─────────────

def test_round1_prompt_has_no_stretch_permission(monkeypatch):
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    captured = {}

    def _fake_llm(system_prompt, user_message, **k):
        captured["user_message"] = user_message
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    verdict_refine._rewrite_round(
        _base_content(), "job text", "feedback", round_num=1, kind="honest"
    )
    msg = captured["user_message"]
    assert "STRETCH ESCALATION" not in msg
    assert "Atruvia" not in msg
    assert "stretch_additions" not in msg


def test_stretch_round_prompt_has_stretch_permission_and_protected_employers(monkeypatch):
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    captured = {}

    def _fake_llm(system_prompt, user_message, **k):
        captured["user_message"] = user_message
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    verdict_refine._rewrite_round(
        _base_content(), "job text", "feedback", round_num=3, kind="stretch"
    )
    msg = captured["user_message"]
    assert "STRETCH ESCALATION" in msg
    assert "stretch_additions" in msg
    # Protected employers + flexible Altoros projects must both be named.
    for employer in ("Atruvia", "Fairmarkit", "Intel", "SII", "SolbegSoft"):
        assert employer in msg
    assert "E-commerce" in msg


def test_full_loop_round3_runs_as_stretch_and_merges_to_learn(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        if "STRETCH ESCALATION" in user_message:
            return {"resume_en": _resume(), "stretch_additions": ["Vitest"]}
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)

    verdict_sequence = iter(
        [_v(80, missing=["Docker"]), _v(85, missing=["Docker"]), _v(90)]
    )
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content(to_learn="")
    verdict = _v(70, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job needs Docker", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=3,
    )

    assert len(calls) == 3
    assert "STRETCH ESCALATION" not in calls[0]
    assert "STRETCH ESCALATION" not in calls[1]
    assert "STRETCH ESCALATION" in calls[2]
    assert out_content["to_learn"] == "Vitest"
    assert out_verdict["score"] == 90


def test_two_round_loop_never_stretches(tmp_path, monkeypatch):
    """With max_rounds=2 the loop stays honest — stretch only ever runs from
    round STRETCH_FROM_ROUND (3), the owner's 'openly add skills' round."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)

    verdict_sequence = iter([_v(80, missing=["Docker"]), _v(85, missing=["Docker"])])
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content(to_learn="")
    verdict = _v(70, missing=["Docker"])
    refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=2,
    )
    assert len(calls) == 2
    assert all("STRETCH ESCALATION" not in c for c in calls)


def test_round2_not_run_when_round1_reaches_target(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(97)
    )

    content = _base_content()
    verdict = _v(70, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=2,
    )
    assert len(calls) == 1  # round 2 never ran
    assert out_verdict["score"] == 97


# ── refine_loop: M1 escalate-after-rollback (docs/LLM_COST_REDUCTION_PLAN.md) ──

def test_round1_rollback_escalates_round2_to_stretch(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    # Round 1 rolls back (75 < 80); round 2 (escalated stretch) accepted (90 > 80).
    verdict_sequence = iter([_v(75), _v(90)])
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=2,
    )

    assert len(calls) == 2
    assert "STRETCH ESCALATION" not in calls[0]
    assert "STRETCH ESCALATION" in calls[1]
    assert out_verdict["score"] == 90


def test_round2_accepted_resets_escalation_round3_stays_honest(tmp_path, monkeypatch):
    """A round that ends in acceptance resets the flag: round 3 is naturally
    stretch anyway (STRETCH_FROM_ROUND=3), but a round accepted mid-way must
    not force an unrelated later honest round to escalate."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    # Round 1 rolls back, round 2 (escalated stretch) accepted, round 3 never
    # runs because target is reached.
    verdict_sequence = iter([_v(75), _v(97)])
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=3,
    )
    assert len(calls) == 2  # round 3 never ran — already at target
    assert out_verdict["score"] == 97


def test_honest_rollback_then_stretch_rollback_stops_loop(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    # Both rounds roll back (score never improves): 75, then 78 (still < 80).
    verdict_sequence = iter([_v(75), _v(78)])
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=4,
    )

    # Round 1 (honest, rollback) + round 2 (escalated stretch, rollback) —
    # then the loop stops; rounds 3-4 never run.
    assert len(calls) == 2
    assert "STRETCH ESCALATION" not in calls[0]
    assert "STRETCH ESCALATION" in calls[1]
    assert out_content is content
    assert out_verdict is verdict


def test_max_rounds_one_unaffected_by_escalation_logic(tmp_path, monkeypatch):
    """max_rounds=1 behaviour is byte-for-byte unchanged: a single honest
    round runs regardless of the (nonexistent) prior-round outcome."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    calls = []

    def _fake_llm(system_prompt, user_message, **k):
        calls.append(user_message)
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    monkeypatch.setattr(ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(75))

    content = _base_content()
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=1,
    )
    assert len(calls) == 1
    assert "STRETCH ESCALATION" not in calls[0]
    assert out_content is content
    assert out_verdict is verdict


def test_escalated_stretch_round_merges_to_learn(tmp_path, monkeypatch):
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())

    def _fake_llm(system_prompt, user_message, **k):
        if "STRETCH ESCALATION" in user_message:
            return {"resume_en": _resume(), "stretch_additions": ["Vitest"]}
        return {"resume_en": _resume()}

    monkeypatch.setattr(llm_client, "call_llm", _fake_llm)
    verdict_sequence = iter([_v(75), _v(90)])
    monkeypatch.setattr(
        ats_pdf_roundtrip,
        "run_llm_verdict",
        lambda folder, job_text: next(verdict_sequence),
    )

    content = _base_content(to_learn="")
    verdict = _v(80, missing=["Docker"])
    out_content, out_verdict = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=2,
    )
    assert out_content["to_learn"] == "Vitest"
    assert out_verdict["score"] == 90


# ── _merge_to_learn ────────────────────────────────────────────────────────────

def test_merge_to_learn_dedupes_and_preserves_existing():
    content = {"to_learn": "Existing skill"}
    verdict_refine._merge_to_learn(content, ["Vitest", "Existing skill", "Tailwind"])
    assert content["to_learn"] == "Existing skill, Vitest, Tailwind"


def test_merge_to_learn_noop_when_no_additions():
    content = {"to_learn": "X"}
    verdict_refine._merge_to_learn(content, None)
    assert content["to_learn"] == "X"
    verdict_refine._merge_to_learn(content, [])
    assert content["to_learn"] == "X"


def test_round1_never_touches_to_learn(tmp_path, monkeypatch):
    """Round 1 (honest) has no stretch_additions field — to_learn is untouched
    even if the LLM response includes one (defence in depth)."""
    _patch_safety_stages(monkeypatch)
    monkeypatch.setattr(llm_profiles, "get_active", lambda: _fake_profile())
    monkeypatch.setattr(
        llm_client,
        "call_llm",
        lambda *a, **k: {"resume_en": _resume(), "stretch_additions": ["Should be ignored"]},
    )
    monkeypatch.setattr(
        ats_pdf_roundtrip, "run_llm_verdict", lambda folder, job_text: _v(90)
    )
    content = _base_content(to_learn="")
    verdict = _v(80, missing=["Docker"])
    out_content, _ = refine_loop(
        content, "job", "", tmp_path, verdict,
        regenerate_docs=lambda f: None, target=95, max_rounds=1,
    )
    assert out_content["to_learn"] == ""
