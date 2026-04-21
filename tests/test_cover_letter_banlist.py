"""Banned opener patterns — ensure pretentious / boilerplate openings are rejected."""

from apply_agent import _opener_banlist_hits


# ── should TRIGGER the banlist ───────────────────────────────────────────────


def test_banlist_thought_leadership_lecture() -> None:
    letter = (
        "The best frontend engineers I know don't just ship features — they guard "
        "the experience. When I read about Egnyte's UX Team role, that framing resonated."
    )
    assert _opener_banlist_hits(letter), "thought-leadership opener must be flagged"


def test_banlist_is_what_i_bring_to() -> None:
    letter = (
        "Ten years of building high-performance Angular applications - "
        "from AngularJS roots to Angular 19 in production - is what I bring "
        "to the Angular Developer role at Marktine Technology Solutions."
    )
    assert _opener_banlist_hits(letter)


def test_banlist_is_exactly_what_x_requires() -> None:
    letter = (
        "Ten years of shipping high-performance enterprise frontends for financial "
        "and procurement clients is exactly what Madiff's programme requires."
    )
    assert _opener_banlist_hits(letter)


def test_banlist_exactly_the_challenges() -> None:
    letter = (
        "Your Angular frontend role caught my attention because I've spent the last "
        "two years solving exactly the challenges you're facing."
    )
    assert _opener_banlist_hits(letter)


def test_banlist_i_am_writing_to() -> None:
    assert _opener_banlist_hits("I am writing to express my interest in the role.")


def test_banlist_i_am_excited_to() -> None:
    assert _opener_banlist_hits("I am excited to apply for the Senior Angular role.")


def test_banlist_as_a_self_label() -> None:
    assert _opener_banlist_hits("As a highly-skilled Angular developer with 10 years…")


def test_banlist_engineering_teams_succeed() -> None:
    assert _opener_banlist_hits("Engineering teams succeed when they move fast.")


# ── should NOT trigger (good openers following Shape B) ──────────────────────


def test_good_opener_concrete_fact_about_them_angular_19_signals() -> None:
    letter = (
        "Your posting lists Angular 19 + Signals + AG Grid — that's the exact stack "
        "I've been shipping at Fairmarkit for the past year."
    )
    assert _opener_banlist_hits(letter) == []


def test_good_opener_references_banking_domain() -> None:
    letter = (
        "Since your team works on loan-processing dashboards for banks, the two "
        "Angular apps I built at Venture Labs for 300+ German cooperative banks "
        "are directly relevant."
    )
    assert _opener_banlist_hits(letter) == []


def test_good_opener_role_anchored_with_specific_reason() -> None:
    letter = (
        "I'm applying for the Senior Angular role at Madiff because the "
        "'Angular 17+ on enterprise procurement' line in your posting matches "
        "the last two years of my work at Fairmarkit."
    )
    assert _opener_banlist_hits(letter) == []


def test_good_opener_concrete_migration_fact() -> None:
    letter = (
        "Last year I migrated Venture Labs' Angular 14 banking platform to "
        "Angular 19, shipping it to 300+ German cooperative banks."
    )
    assert _opener_banlist_hits(letter) == []
