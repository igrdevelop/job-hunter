"""Tests for hunter.location_parse (docs/MARKET_MEMORY_PLAN.md M1.b).

Home-city expectations are built from the SAME accessor the module uses
(``filter_profile._home_city_aliases``) — the owner's city is never spelled
out here, and the module itself must contain no city literal at all.
"""

from __future__ import annotations

import pytest

from hunter import filter_profile
from hunter.filters import _PL_ANTI_HYBRID_CITIES
from hunter.location_parse import (
    HYBRID,
    ONSITE,
    REMOTE,
    UNKNOWN,
    LocationParse,
    _fold,
    classify_location,
)


def _home_alias() -> str:
    """One of the candidate's own home-city aliases, folded like the module."""
    aliases = sorted(_fold(a) for a in filter_profile._home_city_aliases())
    assert aliases, "candidate profile exposes no home-city aliases"
    return aliases[0]


def _pl_city(exclude_home: bool = True) -> str:
    """A Polish anti-hybrid city that is NOT the candidate's home city."""
    home = {_fold(a) for a in filter_profile._home_city_aliases()}
    for c in sorted(_PL_ANTI_HYBRID_CITIES):
        if not exclude_home or _fold(c) not in home:
            return c
    raise AssertionError("no non-home PL city in the vocabulary")


# ── remote_mode ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Remote", REMOTE),
        ("Remote — Poland, Germany", REMOTE),
        ("Remote (Poland)", REMOTE),
        ("Anywhere", REMOTE),
        ("Worldwide", REMOTE),
        ("Anywhere in the world", REMOTE),
        ("Global", REMOTE),
        ("Polska (zdalnie)", REMOTE),
        ("Praca zdalna", REMOTE),
        ("100% remote", REMOTE),
        ("Fully remote", REMOTE),
        ("Home office", REMOTE),
        ("Work from home", REMOTE),
        ("WFH", REMOTE),
        ("Hybrid", HYBRID),
        ("Hybrid, Remote possible", HYBRID),
        ("Berlin (Hybrid)", HYBRID),
        ("Praca hybrydowa", HYBRID),
        ("hybrydowo", HYBRID),
        ("On-site", ONSITE),
        ("onsite", ONSITE),
        ("On site", ONSITE),
        ("London, UK (On-site)", ONSITE),
        ("Praca stacjonarna", ONSITE),
        ("In office", ONSITE),
        ("Office-based", ONSITE),
        ("In-office", ONSITE),
        ("Hybrid / on-site", HYBRID),  # hybrid outranks onsite
        ("On-site, remote possible", ONSITE),  # onsite outranks remote
        ("Unknown", UNKNOWN),
        ("Remoteleaf HQ", UNKNOWN),  # word boundary: "Remoteleaf" is not "remote"
        ("Poland", UNKNOWN),
        ("", UNKNOWN),
        (None, UNKNOWN),
        ("   ", UNKNOWN),
    ],
)
def test_remote_mode(raw, expected):
    assert classify_location(raw).remote_mode == expected


# ── city + is_home_city ──────────────────────────────────────────────────────


def test_home_city_plain():
    alias = _home_alias()
    p = classify_location(alias.title())
    assert p.city == alias
    assert p.is_home_city is True


def test_home_city_with_hybrid_marker():
    alias = _home_alias()
    p = classify_location(f"{alias.title()} (Hybrid)")
    assert p == LocationParse(
        raw=f"{alias.title()} (Hybrid)", remote_mode=HYBRID, city=alias, is_home_city=True
    )


def test_home_city_diacritics_insensitive():
    # Every alias variant (with or without diacritics) folds to the same key.
    aliases = {_fold(a) for a in filter_profile._home_city_aliases()}
    for raw in filter_profile._home_city_aliases():
        p = classify_location(raw.upper())
        assert p.city in aliases
        assert p.is_home_city is True


def test_wroclaw_style_no_diacritic_matches_diacritic_alias():
    # Whatever the home city is, its ASCII-folded spelling must match too.
    alias = _home_alias()
    ascii_form = _fold(alias)
    p = classify_location(ascii_form)
    assert p.city == ascii_form
    assert p.is_home_city is True


def test_city_from_pl_anti_hybrid_set_is_not_home():
    city = _pl_city()
    p = classify_location(f"{city.title()} (On-site)")
    assert p.city == _fold(city)
    assert p.is_home_city is False
    assert p.remote_mode == ONSITE


def test_city_without_mode_token_is_unknown_not_onsite():
    city = _pl_city()
    p = classify_location(city.title())
    assert p.city == _fold(city)
    assert p.remote_mode == UNKNOWN


def test_leftmost_city_wins():
    cities = sorted(c for c in _PL_ANTI_HYBRID_CITIES if c.isalpha())
    a, b = cities[0], cities[-1]
    assert classify_location(f"{a.title()}, {b.title()}").city == _fold(a)
    assert classify_location(f"{b.title()}, {a.title()}").city == _fold(b)


def test_locative_declension_matches_when_vocabulary_lists_it():
    # The PL set lists locative forms ("w Warszawie"-style). No nominative
    # mapping exists in the vocabulary, so the folded matched form is returned.
    declensions = sorted(c for c in _PL_ANTI_HYBRID_CITIES if c.endswith("ie"))
    assert declensions, "vocabulary no longer lists locative forms"
    form = declensions[0]
    p = classify_location(f"w {form.title()}")
    assert p.city == _fold(form)


def test_office_in_city_reads_city_not_onsite():
    # "Office in <city>" carries no mode token from the fixed set — the city is
    # extracted but the mode stays unknown (never guessed).
    city = _pl_city()
    p = classify_location(f"Office in {city.title()}")
    assert p.city == _fold(city)
    assert p.remote_mode == UNKNOWN


def test_city_diacritics_insensitive():
    # A vocabulary entry with a diacritic must match its ASCII spelling and
    # vice versa (the set lists both for PL cities; use the diacritic one).
    with_diacritics = sorted(c for c in _PL_ANTI_HYBRID_CITIES if _fold(c) != c)
    assert with_diacritics
    entry = with_diacritics[0]
    assert classify_location(entry.upper()).city == _fold(entry)
    assert classify_location(_fold(entry).upper()).city == _fold(entry)


def test_city_word_boundary():
    # A city name glued into a longer token is not a city mention.
    city = _pl_city()
    assert classify_location(f"{city}ville-corp").city == ""
    assert classify_location(f"x{city}").city == ""


def test_no_city_for_remote_only_strings():
    for raw in ("Remote", "Anywhere", "Worldwide", "Unknown", "Poland"):
        assert classify_location(raw).city == ""


def test_custom_flt_extra_anti_hybrid_cities_is_honoured():
    flt = {"extra_anti_hybrid_cities": ["Ruritania City"]}
    p = classify_location("Ruritania City (Hybrid)", flt=flt)
    assert p == LocationParse(
        raw="Ruritania City (Hybrid)", remote_mode=HYBRID, city="ruritania city", is_home_city=False
    )
    # Same string against the default profile: the city is not in its vocabulary.
    assert classify_location("Ruritania City (Hybrid)").city == ""
    # Multi-word entries tolerate whitespace runs and case.
    assert classify_location("RURITANIA   city", flt=flt).city == "ruritania city"


def test_custom_flt_extras_do_not_make_home_city():
    flt = {"extra_anti_hybrid_cities": ["Ruritania City"]}
    assert classify_location("Ruritania City", flt=flt).is_home_city is False


def test_foreign_city_via_builtin_profile_extras():
    # The builtin profile already carries foreign extras (e.g. for
    # "Berlin (Hybrid)"). Pick whatever is there rather than assuming a name.
    from hunter.config import FILTER

    extras = [str(c) for c in (FILTER.get("extra_anti_hybrid_cities") or [])]
    if not extras:
        pytest.skip("builtin profile has no extra_anti_hybrid_cities")
    p = classify_location(f"{extras[0].title()} (Hybrid)")
    assert p.city == _fold(extras[0])
    assert p.remote_mode == HYBRID
    assert p.is_home_city is False


# ── raw + robustness ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected_raw",
    [
        ("  Remote  ", "Remote"),
        ("", ""),
        (None, ""),
        ("\tHybrid\n", "Hybrid"),
    ],
)
def test_raw_is_stripped(raw, expected_raw):
    assert classify_location(raw).raw == expected_raw


@pytest.mark.parametrize("raw", [123, 4.5, ["Remote"], {"a": 1}, object()])
def test_never_raises_on_non_string(raw):
    p = classify_location(raw)  # type: ignore[arg-type]
    assert isinstance(p, LocationParse)


def test_result_is_frozen():
    p = classify_location("Remote")
    with pytest.raises(AttributeError):
        p.city = "x"  # type: ignore[misc]


def test_fold_collapses_case_diacritics_whitespace():
    assert _fold("  Łódź   Ą  ") == "lodz a"
