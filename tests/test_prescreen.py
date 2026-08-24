"""The stack pre-screen's parsing and its verbatim-quote defence.

docs/STACK_PRESCREEN_PLAN.md M3. The model call itself is calibrated offline by
tools/prescreen_calibrate.py against the real corpus; what needs guarding in CI
is everything around it — because every one of these paths ends in "skip this
vacancy without generating", and a wrong answer there costs a real application.
"""

from hunter.prescreen import PrescreenVerdict, assess_stack, evidence_is_verbatim, parse_verdict

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

    def test_a_failing_call_is_swallowed(self, monkeypatch):
        def _boom(**_kw):
            raise RuntimeError("provider down")

        monkeypatch.setattr("llm_client.call_llm", _boom)

        v = assess_stack(POSTING)
        assert isinstance(v, PrescreenVerdict)
        assert v.ok is False, "an unavailable pre-screen must never block a vacancy"

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
