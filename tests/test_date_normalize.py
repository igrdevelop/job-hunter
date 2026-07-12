"""Unit tests for hunter.date_normalize.normalize_period.

Taleo + some legacy ATS parsers expect MM/YYYY periods. The renderer normalizes
period strings the LLM wrote ("Jan 2020 – Mar 2024") to the strict form before
writing them into the docx, so the LLM prompt stays free of date-formatting
rules and Workday/Greenhouse don't lose roles to a parsing mismatch.
"""

import pytest

from hunter.date_normalize import normalize_period


@pytest.mark.parametrize(
    "raw,expected",
    [
        # English month names — short
        ("Jan 2020 – Mar 2024", "01/2020 – 03/2024"),
        ("Aug 2023 – Present", "08/2023 – Present"),
        ("Feb 2019 – Jul 2022", "02/2019 – 07/2022"),
        # English month names — full
        ("January 2020 – March 2024", "01/2020 – 03/2024"),
        ("August 2023 – Present", "08/2023 – Present"),
        # Mixed dashes
        ("Mar 2018 - Aug 2020", "03/2018 – 08/2020"),
        ("Mar 2018 — Aug 2020", "03/2018 – 08/2020"),
        # Already in canonical form
        ("01/2020 – 03/2024", "01/2020 – 03/2024"),
        # Single-digit MM normalises to two-digit MM
        ("3/2024 – 12/2024", "03/2024 – 12/2024"),
        # Present synonyms
        ("Jan 2020 – Current", "01/2020 – Present"),
        ("Jan 2020 – Now", "01/2020 – Present"),
        # Polish month names (PL CV reuses the same render path)
        ("Sty 2020 – Mar 2024", "01/2020 – 03/2024"),
        ("Marca 2020 – Sierpnia 2024", "03/2020 – 08/2024"),
        ("Lipiec 2019 – Obecnie", "07/2019 – Present"),
    ],
)
def test_normalize_period_strict_form(raw: str, expected: str) -> None:
    assert normalize_period(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "2020 – 2024",  # year-only — don't fabricate months
        "2020 – Present",
        "",
        "   ",
        "freelance / consulting",  # no recognisable date — pass through
        "Q3 2024 – Q1 2025",  # quarter — not parsed
    ],
)
def test_normalize_period_passes_through_unparseable(raw: str) -> None:
    # Either the original string back, or "Present" canonicalized — never a
    # garbage substitution that loses information.
    out = normalize_period(raw)
    assert out == raw or "Present" in out


def test_normalize_period_handles_none_safely() -> None:
    # The renderer hands us whatever's in the JSON, so guard against junk types.
    assert normalize_period(None) == ""  # type: ignore[arg-type]


def test_normalize_period_year_only_left_unchanged() -> None:
    # Year-only periods are LEFT UNCHANGED. We do not invent January/December
    # bounds — Taleo accepts YYYY, and silently inflating "2020 – 2024" to
    # "01/2020 – 12/2024" would misrepresent the actual employment dates.
    assert normalize_period("2020 – 2024") == "2020 – 2024"


def test_normalize_period_single_date_no_dash() -> None:
    # A single date (no range) — e.g. when the LLM writes just the start.
    assert normalize_period("Mar 2024") == "03/2024"
    assert normalize_period("March 2024") == "03/2024"


def test_normalize_period_preserves_present_capitalization() -> None:
    # Output is always "Present" (capitalised) regardless of input case —
    # one canonical token avoids ATS regex inconsistency.
    assert normalize_period("Jan 2020 – present") == "01/2020 – Present"
    assert normalize_period("Jan 2020 – CURRENT") == "01/2020 – Present"
