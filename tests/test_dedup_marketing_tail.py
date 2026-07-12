"""
Tests for P-1.1: _strip_marketing_tail() in dedup_key.

Gmail job-alert enrichers append marketing copy after the real title.
These tests verify that dedup_key() collapses enriched and original titles
into the same key, while not mangling legitimate tech-stack separators.
"""

import pytest
from hunter.tracker import dedup_key, _strip_marketing_tail  # noqa: F401 (private import OK in tests)


# ---------------------------------------------------------------------------
# _strip_marketing_tail — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        # em-dash separator + marketing verb
        ("Angular Developer — Build High-Performance Frontends", "Angular Developer"),
        ("Senior Frontend Engineer — Shape the future of fintech", "Senior Frontend Engineer"),
        ("React Developer — Transform our digital platform", "React Developer"),
        # en-dash separator + marketing verb
        ("Frontend Dev – Join a fast-growing team", "Frontend Dev"),
        ("Angular Engineer – Craft beautiful UIs", "Angular Engineer"),
        # pipe separator (no verb requirement)
        ("Senior Frontend Engineer | Help us shape the future", "Senior Frontend Engineer"),
        ("Angular Developer | Wrocław", "Angular Developer"),
        # hyphen + marketing verb
        ("Frontend Developer - Join a team that ships", "Frontend Developer"),
        ("Angular Dev - Build scalable SPAs", "Angular Dev"),
        # no separator — unchanged
        ("Angular Developer", "Angular Developer"),
        ("Senior Frontend Developer", "Senior Frontend Developer"),
    ],
)
def test_strip_marketing_tail(raw: str, expected: str) -> None:
    assert _strip_marketing_tail(raw) == expected, f"Input: {raw!r}"


def test_strip_marketing_tail_preserves_tech_stack_dash() -> None:
    """'Angular – React Developer' must NOT be stripped (tech separator, not marketing)."""
    title = "Angular – React Developer"
    assert _strip_marketing_tail(title) == title


def test_strip_marketing_tail_preserves_hyphenated_tech() -> None:
    """'Full-Stack Developer' hyphen is part of the word — no strip."""
    title = "Full-Stack Developer"
    assert _strip_marketing_tail(title) == title


# ---------------------------------------------------------------------------
# dedup_key — Gmail marketing tail collapses to same key
# ---------------------------------------------------------------------------


def test_dedup_gmail_em_dash_same_as_original() -> None:
    """Gmail enriched form must match original tracker entry."""
    original = dedup_key("Acme", "Angular Developer")
    enriched = dedup_key("Acme", "Angular Developer — Build High-Performance Frontends")
    assert original == enriched


def test_dedup_gmail_pipe_same_as_original() -> None:
    original = dedup_key("Acme", "Senior Frontend Engineer")
    enriched = dedup_key("Acme", "Senior Frontend Engineer | Help us shape the future")
    assert original == enriched


def test_dedup_gmail_en_dash_verb_same_as_original() -> None:
    original = dedup_key("Acme", "Frontend Dev")
    enriched = dedup_key("Acme", "Frontend Dev – Join a fast-growing team")
    assert original == enriched


def test_dedup_distinct_roles_stay_distinct() -> None:
    """Stripping must not collapse genuinely different roles."""
    k1 = dedup_key("Acme", "Angular Developer")
    k2 = dedup_key("Acme", "Backend Developer")
    assert k1 != k2


def test_dedup_tech_stack_dash_stays_distinct() -> None:
    """'Angular – React Developer' and 'Angular Developer' are different roles."""
    k1 = dedup_key("Acme", "Angular – React Developer")
    k2 = dedup_key("Acme", "Angular Developer")
    # After strip (no verb lookahead match), both normalize differently — they should differ
    assert k1 != k2
