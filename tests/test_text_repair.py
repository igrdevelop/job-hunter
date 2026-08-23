"""Tests for hunter/text_repair.py — the seam left behind by a clause drop.

The four `TestShippedArtifacts` cases are the exact defects the owner found in a
RENDERED, delivered CV/cover letter (Avive Solutions, 2026-08-20); each one is
reproduced through the production entry point `claim_judge._drop_quote`, not
just the helper, so the test fails if the wiring is undone as well as if the
helper regresses.
"""

from __future__ import annotations

from hunter import text_repair
from hunter.apply_shared import _scrub_compliance_clause, _scrub_prestige_text, _prestige_claim_re
from hunter.claim_judge import _drop_quote


class TestShippedArtifacts:
    def test_no_dangling_comma_before_dash(self):
        """Observed: '... HTTP interceptors, - from a real-time incident ...'"""
        text = (
            "Deep expertise in TypeScript, RxJS, Angular Signals, routing guards, "
            "HTTP interceptors, and Sentry error monitoring - from a real-time "
            "incident mapping system to 2 greenfield banking apps."
        )
        out = _drop_quote(text, "Sentry error monitoring")
        assert ", -" not in out
        assert out.startswith("Deep expertise in TypeScript")
        assert "HTTP interceptors - from a real-time" in out

    def test_cover_letter_keeps_its_paragraphs(self):
        """A single dropped clause used to flatten the WHOLE letter: `\\s{2,}`
        matches newlines, so every blank line became one space."""
        text = (
            "Dear Hiring Manager,\n\n"
            "I am writing with 10+ years of Angular, Signals, and Fortune 500 "
            "delivery experience.\n\n"
            "At Venture Labs I built two apps.\n\n"
            "Best regards."
        )
        out = _drop_quote(text, "Fortune 500 delivery experience")
        assert "Fortune 500" not in out
        assert out.count("\n\n") == 3

    def test_survivor_of_a_leading_cut_is_recapitalized(self):
        """Observed: a bullet rendered as 'enforced comprehensive test coverage'."""
        text = (
            "Designed Jenkins CI/CD pipelines; enforced comprehensive test coverage "
            "using Jest unit tests and Cypress E2E suites across all applications."
        )
        out = _drop_quote(text, "Designed Jenkins CI/CD pipelines;")
        assert out.startswith("Enforced comprehensive test coverage")

    def test_no_semicolon_before_a_fragment(self):
        """Observed: '... architecture decisions; across a team of 10+.'"""
        text = (
            "Led Angular version upgrades (10 to 12) and contributed to frontend "
            "architecture decisions; mentored 2 junior developers across a team of 10+."
        )
        out = _drop_quote(text, "mentored 2 junior developers")
        assert ";" not in out
        assert out.endswith("architecture decisions across a team of 10+.")


class TestCollapseSpaces:
    def test_collapses_runs_of_spaces(self):
        assert text_repair.collapse_spaces("a    b") == "a b"

    def test_preserves_newlines(self):
        assert text_repair.collapse_spaces("a\n\nb") == "a\n\nb"

    def test_trims_space_around_a_break(self):
        assert text_repair.collapse_spaces("a   \n   b") == "a\nb"


class TestRepairJunction:
    def test_drops_our_separator_when_the_right_side_carries_one(self):
        assert (
            text_repair.repair_junction("Built 2 apps,", ". Then more.")
            == "Built 2 apps. Then more."
        )

    def test_keeps_an_ordinary_comma_before_a_continuation(self):
        out = text_repair.repair_junction("Shipped the module,", "on time.")
        assert out == "Shipped the module, on time."

    def test_strips_a_stranded_trailing_connector(self):
        assert text_repair.repair_junction("Angular and", "TypeScript.") == "Angular TypeScript."

    def test_empty_left_side_strips_the_leading_separator(self):
        assert text_repair.repair_junction("", "- shipped it.") == "shipped it."

    def test_empty_left_side_does_not_eat_the_first_letter(self):
        """Regression: `[,;:.-–—]` built by interpolation made '.-–' a RANGE
        covering every ASCII letter, so the leading character was stripped."""
        assert text_repair.repair_junction("", "enforced coverage.") == "enforced coverage."
        assert text_repair.repair_junction("", "Shipped it.") == "Shipped it."

    def test_empty_right_side_trims_a_dangling_separator(self):
        assert text_repair.repair_junction("Built 2 apps,", "") == "Built 2 apps"

    def test_capitalizes_a_new_sentence_start(self):
        out = text_repair.repair_junction("Built 2 apps.", "then migrated them.")
        assert out == "Built 2 apps. Then migrated them."


class TestDropSentences:
    def test_keeps_paragraph_breaks(self):
        text = "One. Two bad.\n\nThree."
        out = text_repair.drop_sentences(text, lambda s: "bad" in s)
        assert out == "One.\n\nThree."

    def test_drops_the_matching_sentence_only(self):
        text = "Alpha. Beta bad. Gamma."
        assert text_repair.drop_sentences(text, lambda s: "bad" in s) == "Alpha. Gamma."


class TestScrubsShareTheFix:
    def test_compliance_scrub_repairs_its_seam(self):
        text = "Delivered the banking module with DORA compliance, on schedule."
        out = _scrub_compliance_clause(text)
        assert "DORA" not in out
        assert ", ," not in out
        assert out == "Delivered the banking module, on schedule."

    def test_prestige_scrub_keeps_paragraphs(self):
        claim_re = _prestige_claim_re("")
        text = "Led the rollout.\n\nWorked with Fortune 500 clients. Shipped on time."
        out = _scrub_prestige_text(text, claim_re)
        assert "Fortune 500" not in out
        assert "\n\n" in out
