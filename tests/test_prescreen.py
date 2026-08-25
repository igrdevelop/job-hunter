"""The stack pre-screen's parsing and its verbatim-quote defence.

docs/STACK_PRESCREEN_PLAN.md M3. The model call itself is calibrated offline by
tools/prescreen_calibrate.py against the real corpus; what needs guarding in CI
is everything around it — because every one of these paths ends in "skip this
vacancy without generating", and a wrong answer there costs a real application.
"""

from hunter.prescreen import (
    PrescreenVerdict,
    assess_stack,
    evidence_is_verbatim,
    parse_verdict,
    should_skip,
)

POSTING = (
    "Senior Frontend Developer\n\n"
    "We build our product in React and TypeScript, with Next.js for the "
    "public site. You will own the component library and mentor two engineers. "
    "Angular experience is a nice-to-have — one legacy admin panel still uses it. "
    "Fully remote within Poland."
)


def _raw(**overrides) -> dict:
    base = {
        "primary_stack": "react",
        "secondary": ["next.js"],
        "angular_required": False,
        "seniority": "senior",
        "verdict": "mismatch",
        "confidence": 0.9,
        "evidence": "We build our product in React and TypeScript",
    }
    base.update(overrides)
    return base


class TestVerbatimQuote:
    def test_an_exact_quote_passes(self):
        assert evidence_is_verbatim("We build our product in React", POSTING)

    def test_rewrapped_whitespace_still_passes(self):
        # The model re-wraps lines; that must not invalidate a real quote.
        assert evidence_is_verbatim("public site.  You will own\n the component library", POSTING)

    def test_a_paraphrase_fails(self):
        assert not evidence_is_verbatim("The product is built using React", POSTING)

    def test_an_empty_or_tiny_quote_fails(self):
        assert not evidence_is_verbatim("", POSTING)
        assert not evidence_is_verbatim("React", POSTING)


class TestParsing:
    def test_a_well_formed_response(self):
        v = parse_verdict(_raw(), POSTING)
        assert v.ok and v.is_mismatch
        assert v.primary_stack == "react"
        assert v.secondary == ["next.js"]
        assert v.confidence == 0.9

    def test_a_json_string_is_accepted(self):
        import json

        v = parse_verdict(json.dumps(_raw()), POSTING)
        assert v.ok and v.is_mismatch

    def test_an_invented_quote_strips_the_authority(self):
        # The verdict is kept for the record but can no longer skip anything:
        # a model asked to justify a finding will invent the sentence.
        v = parse_verdict(_raw(evidence="This role is 100% React with no Angular"), POSTING)
        assert v.primary_stack == "react"
        assert v.ok is False
        assert v.is_mismatch is False

    def test_an_unknown_verdict_value_is_unusable(self):
        assert parse_verdict(_raw(verdict="probably not"), POSTING).ok is False

    def test_garbage_shapes_are_unusable(self):
        for raw in (None, [], "not json at all", {"nope": 1}, 42):
            assert parse_verdict(raw, POSTING).ok is False

    def test_confidence_is_clamped_and_never_raises(self):
        assert parse_verdict(_raw(confidence=7), POSTING).confidence == 1.0
        assert parse_verdict(_raw(confidence=-3), POSTING).confidence == 0.0
        assert parse_verdict(_raw(confidence="high"), POSTING).confidence == 0.0

    def test_a_fit_verdict_is_never_a_mismatch(self):
        v = parse_verdict(_raw(verdict="fit", primary_stack="angular"), POSTING)
        assert v.ok and not v.is_mismatch


class TestCallBoundary:
    def test_a_short_posting_is_not_worth_a_call(self, monkeypatch):
        called = []
        monkeypatch.setattr("llm_client.call_llm", lambda **kw: called.append(kw) or {})

        assert assess_stack("Frontend dev wanted.").ok is False
        assert called == [], "no model call for text there is nothing to read in"

    def test_a_failing_call_re_raises_for_best_effort(self, monkeypatch):
        # Swallowing it here would make `best_effort("apply.prescreen")` around
        # the caller decorative: a permanently dead pre-screen (revoked key, CLI
        # logged out, model retired) would degrade silently forever while the
        # plan claimed the wrapper was the mitigation. CLAUDE.md states the rule
        # — re-raise from the except clause so the failure reaches best_effort.
        # The vacancy is still never blocked; run_prescreen returns False (see
        # TestPipelineStage::test_a_broken_call_lets_the_vacancy_through).
        import pytest

        def _boom(**_kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr("llm_client.call_llm", _boom)

        with pytest.raises(RuntimeError):
            assess_stack(POSTING)

    def test_the_title_reaches_the_model(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("llm_client.call_llm", lambda **kw: seen.update(kw) or _raw())

        assess_stack(POSTING, title="Regular Frontend Developer/ka")

        assert "Regular Frontend Developer/ka" in seen["user_message"]
        assert POSTING[:40] in seen["user_message"]

    def test_it_uses_the_cheap_judge_model(self, monkeypatch):
        from hunter.config import JUDGE_MODEL

        seen = {}
        monkeypatch.setattr("llm_client.call_llm", lambda **kw: seen.update(kw) or _raw())

        assess_stack(POSTING)

        assert seen["model"] == JUDGE_MODEL, "the pre-screen only earns its place at Haiku prices"


class TestTheGateRuleIsReactOnly:
    """The rule is the owner's decision, not the prompt's wider "mismatch".

    Calibration over 81 real postings (2026-08-24) measured both. "Anything not
    Angular" scored 7/7 recall but skipped SIX vacancies the owner had actually
    sent — a Node.js role, a PixiJS game role, a Vue role, GitLab, HeroDevs, and
    an EPAM posting literally titled "Senior Software Engineer with Angular".
    React-only scored the same 7/7 with zero false skips: not one posting the
    model called "react" was ever sent.
    """

    @staticmethod
    def _v(**kw) -> PrescreenVerdict:
        base = {
            "primary_stack": "react",
            "angular_required": False,
            "verdict": "mismatch",
            "confidence": 0.95,
            "ok": True,
        }
        base.update(kw)
        return PrescreenVerdict(**base)

    def test_a_confident_react_posting_is_skipped(self):
        assert should_skip(self._v(), min_confidence=0.9)

    def test_every_other_stack_generates(self):
        # These are the six false skips the wider rule produced, by stack.
        for stack in ("vue", "other", "fullstack", "unclear", "angular", "svelte"):
            assert not should_skip(self._v(primary_stack=stack), min_confidence=0.9), stack

    def test_angular_required_rescues_the_posting(self):
        # EPAM: "Senior Software Engineer with Angular", read as fullstack/react
        # by the model but with Angular a stated requirement. He sent it.
        assert not should_skip(self._v(angular_required=True), min_confidence=0.9)

    def test_a_shaky_verdict_does_not_act(self):
        # Every real skip in the calibration scored >= 0.95, so the floor costs
        # nothing today and refuses a weaker call tomorrow.
        assert not should_skip(self._v(confidence=0.6), min_confidence=0.9)

    def test_an_unusable_verdict_never_acts(self):
        assert not should_skip(self._v(ok=False), min_confidence=0.9)

    def test_a_fit_verdict_never_acts(self):
        assert not should_skip(self._v(verdict="fit"), min_confidence=0.9)


class TestPipelineStage:
    URL = "https://example.com/jobs/react-first"

    @staticmethod
    def _wire(monkeypatch, *, mode="skip", verdict=None, tracks=("angular",)):
        notes = []
        monkeypatch.setattr("hunter.apply_shared.notify", notes.append)
        monkeypatch.setattr("hunter.config.PRESCREEN_ENABLED", True)
        monkeypatch.setattr("hunter.config.PRESCREEN_MODE", mode)
        monkeypatch.setattr("hunter.config.PRESCREEN_MIN_CONFIDENCE", 0.9)
        # filters imported active_tracks at module load, so patch the reader
        # the stage actually calls.
        monkeypatch.setattr("hunter.filters._react_track_active", lambda: "react" in tracks)

        calls = []

        def _assess(text, **kw):
            calls.append(kw)
            return verdict or PrescreenVerdict(
                primary_stack="react",
                verdict="mismatch",
                confidence=0.95,
                evidence="We build our product in React",
                ok=True,
            )

        monkeypatch.setattr("hunter.prescreen.assess_stack", _assess)
        return notes, calls

    def _run(self, **kw):
        from hunter.apply_shared import run_prescreen

        return run_prescreen("x" * 500, self.URL, **kw)

    def test_report_mode_only_logs(self, tracker_db, monkeypatch):
        notes, calls = self._wire(monkeypatch, mode="report")
        assert self._run() is False
        assert calls, "the verdict is still collected — that is the point of report mode"
        assert notes == []

    def test_warn_mode_notifies_but_generates(self, tracker_db, monkeypatch):
        notes, _ = self._wire(monkeypatch, mode="warn")
        assert self._run() is False
        assert len(notes) == 1 and "Pre-screen" in notes[0]

    def test_skip_mode_aborts_and_writes_the_row(self, tracker_db, monkeypatch):
        from hunter import tracker

        notes, _ = self._wire(monkeypatch, mode="skip")
        assert self._run(title="Frontend Dev", company="Acme") is True

        rows = tracker.lookup_url(self.URL)
        assert rows and rows[0]["ats"].strip().upper() == "SKIP"
        assert any("Skipped" in n for n in notes)

    def test_a_manual_request_is_never_skipped(self, tracker_db, monkeypatch):
        notes, _ = self._wire(monkeypatch, mode="skip")
        assert self._run(is_manual=True) is False
        assert any("Generating anyway" in n for n in notes)

    def test_force_is_never_skipped(self, tracker_db, monkeypatch):
        notes, _ = self._wire(monkeypatch, mode="skip")
        assert self._run(is_force_override=True) is False
        assert any("Generating anyway" in n for n in notes)

    def test_the_react_track_makes_it_a_no_op(self, tracker_db, monkeypatch):
        _notes, calls = self._wire(monkeypatch, mode="skip", tracks=("angular", "react"))
        assert self._run() is False
        assert calls == [], "no call at all when React is a stack the candidate applies for"

    def test_a_broken_call_lets_the_vacancy_through(self, tracker_db, monkeypatch):
        self._wire(monkeypatch, mode="skip")

        def _boom(*_a, **_kw):
            raise RuntimeError("judge down")

        monkeypatch.setattr("hunter.prescreen.assess_stack", _boom)
        assert self._run() is False

    def test_disabled_makes_no_call(self, tracker_db, monkeypatch):
        _notes, calls = self._wire(monkeypatch, mode="skip")
        monkeypatch.setattr("hunter.config.PRESCREEN_ENABLED", False)
        assert self._run() is False
        assert calls == []

    def test_an_empty_posting_makes_no_call(self, tracker_db, monkeypatch):
        _notes, calls = self._wire(monkeypatch, mode="skip")
        from hunter.apply_shared import run_prescreen

        assert run_prescreen("", self.URL) is False
        assert calls == []
