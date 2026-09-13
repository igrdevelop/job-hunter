"""
Unit tests for hunter/salary_parse.py — the deterministic ($0) salary parser
(docs/MARKET_MEMORY_PLAN.md M1.b).

The parametrized table carries at least one row per source shape, taken
from what each source's `_parse_salary`/`_format_salary` actually emits
(hunter/sources/*.py), plus the generic free-text forms and the documented
edge rules. Every row asserts the FULL tuple, not one field.
"""

import pytest

from hunter.salary_parse import SalaryParse, parse_salary

# (raw, (min, max, currency, period, contract, monthly_min, monthly_max))
CASES = [
    # --- JustJoin / NoFluffJobs / SmartJobs: monthly, no unit, optional contract
    ("15 000–20 000 PLN B2B", (15000.0, 20000.0, "PLN", "month", "b2b", 15000.0, 20000.0)),
    ("15 000+ PLN UOP", (15000.0, None, "PLN", "month", "uop", 15000.0, None)),
    ("do 20 000 PLN", (None, 20000.0, "PLN", "month", "", None, 20000.0)),
    ("up to 20 000 PLN B2B", (None, 20000.0, "PLN", "month", "b2b", None, 20000.0)),
    ("18 000–24 000 PLN UoD", (18000.0, 24000.0, "PLN", "month", "other", 18000.0, 24000.0)),
    ("15 000–20 000 PLN UZ", (15000.0, 20000.0, "PLN", "month", "other", 15000.0, 20000.0)),
    # --- Inhire
    ("15000-20000 PLN", (15000.0, 20000.0, "PLN", "month", "", 15000.0, 20000.0)),
    # --- theprotocol / pracuj / inhire open bound placeholder
    ("?-20000 PLN", (None, 20000.0, "PLN", "month", "", None, 20000.0)),
    ("15000-? PLN", (15000.0, None, "PLN", "month", "", 15000.0, None)),
    # --- Bulldogjob: "{money} PLN", money is free text
    ("20 000 - 26 000 PLN", (20000.0, 26000.0, "PLN", "month", "", 20000.0, 26000.0)),
    ("20000-26000 PLN", (20000.0, 26000.0, "PLN", "month", "", 20000.0, 26000.0)),
    # --- theprotocol
    ("20 000 - 26 000 zł (B2B)", (20000.0, 26000.0, "PLN", "month", "b2b", 20000.0, 26000.0)),
    (
        "20 000 - 26 000 zł netto (+ VAT) / mies.",
        (20000.0, 26000.0, "PLN", "month", "", 20000.0, 26000.0),
    ),
    ("120 - 150 zł brutto / godz.", (120.0, 150.0, "PLN", "hour", "", 19200.0, 24000.0)),
    # --- Pracuj
    ("20 000–26 000 PLN", (20000.0, 26000.0, "PLN", "month", "", 20000.0, 26000.0)),
    ("120–150 zł / godz.", (120.0, 150.0, "PLN", "hour", "", 19200.0, 24000.0)),
    ("20 000–26 000 PLN brutto / mies.", (20000.0, 26000.0, "PLN", "month", "", 20000.0, 26000.0)),
    # --- RemoteOK
    ("$80 000–$120 000 USD/yr", (80000.0, 120000.0, "USD", "year", "", 6666.67, 10000.0)),
    ("$80 000+ USD/yr", (80000.0, None, "USD", "year", "", 6666.67, None)),
    ("up to $120 000 USD/yr", (None, 120000.0, "USD", "year", "", None, 10000.0)),
    # --- Himalayas: "{lo}–{hi} {cur}/yr", "{lo}+ {cur}/yr", "up to {hi} {cur}/yr"
    ("80 000–120 000 EUR/yr", (80000.0, 120000.0, "EUR", "year", "", 6666.67, 10000.0)),
    ("60 000+ USD/yr", (60000.0, None, "USD", "year", "", 5000.0, None)),
    ("up to 90 000 GBP/yr", (None, 90000.0, "GBP", "year", "", None, 7500.0)),
    # --- Working Nomads: salary_range_short free text (heuristic band -> month)
    ("$100k - $150k", (100000.0, 150000.0, "USD", "month", "", 100000.0, 150000.0)),
    ("$50k", (50000.0, 50000.0, "USD", "month", "", 50000.0, 50000.0)),
    # --- Remotive free text
    ("$90,000 - $120,000/year", (90000.0, 120000.0, "USD", "year", "", 7500.0, 10000.0)),
    ("€60k-80k", (60000.0, 80000.0, "EUR", "month", "", 60000.0, 80000.0)),
    ("Competitive", (None, None, "", "", "", None, None)),
    # --- Generic
    ("15k–20k PLN", (15000.0, 20000.0, "PLN", "month", "", 15000.0, 20000.0)),
    ("€ 5 000 – 7 000 / month", (5000.0, 7000.0, "EUR", "month", "", 5000.0, 7000.0)),
    ("£450/day", (450.0, 450.0, "GBP", "day", "", 9000.0, 9000.0)),
    ("90 PLN/h", (90.0, 90.0, "PLN", "hour", "", 14400.0, 14400.0)),
    ("89.3–119 PLN/h", (89.3, 119.0, "PLN", "hour", "", 14288.0, 19040.0)),
    ("USD 100,000 per year", (100000.0, 100000.0, "USD", "year", "", 8333.33, 8333.33)),
    ("100 000 - 130 000 zł rocznie", (100000.0, 130000.0, "PLN", "year", "", 8333.33, 10833.33)),
    ("od 18 000 do 24 000 PLN", (18000.0, 24000.0, "PLN", "month", "", 18000.0, 24000.0)),
    ("PLN 18–24k", (18000.0, 24000.0, "PLN", "month", "", 18000.0, 24000.0)),
    ("20-26k zl net b2b", (20000.0, 26000.0, "PLN", "month", "b2b", 20000.0, 26000.0)),
    ("6 000 - 8 000 CHF", (6000.0, 8000.0, "CHF", "month", "", 6000.0, 8000.0)),
    ("800 PLN MD", (800.0, 800.0, "PLN", "day", "", 16000.0, 16000.0)),
    ("15000..20000 PLN", (15000.0, 20000.0, "PLN", "month", "", 15000.0, 20000.0)),
    ("15 000 to 20 000 PLN", (15000.0, 20000.0, "PLN", "month", "", 15000.0, 20000.0)),
    ("od 18 000 PLN", (18000.0, None, "PLN", "month", "", 18000.0, None)),
    ("1,5k EUR/h", (1500.0, 1500.0, "EUR", "hour", "", 240000.0, 240000.0)),
    ("120 000 PLN p.a.", (120000.0, 120000.0, "PLN", "year", "", 10000.0, 10000.0)),
    # --- Contract tokens
    (
        "15 000–20 000 PLN umowa o pracę",
        (15000.0, 20000.0, "PLN", "month", "uop", 15000.0, 20000.0),
    ),
    ("10 000 PLN umowa zlecenie", (10000.0, 10000.0, "PLN", "month", "other", 10000.0, 10000.0)),
    ("freelance 8 000 PLN", (8000.0, 8000.0, "PLN", "month", "other", 8000.0, 8000.0)),
    ("5 000 PLN contract", (5000.0, 5000.0, "PLN", "month", "", 5000.0, 5000.0)),  # bare word
    ("kontrakt B2B 22 000 PLN", (22000.0, 22000.0, "PLN", "month", "b2b", 22000.0, 22000.0)),
    # --- Unparsed / degenerate
    (None, (None, None, "", "", "", None, None)),
    ("", (None, None, "", "", "", None, None)),
    ("   ", (None, None, "", "", "", None, None)),
    ("negotiable", (None, None, "", "", "", None, None)),
    ("99 999 999 PLN", (None, None, "PLN", "", "", None, None)),  # absurd -> unparsed
    ("-5000 PLN", (None, None, "PLN", "", "", None, None)),  # negative -> unparsed
    ("0 PLN", (None, None, "PLN", "", "", None, None)),  # zero -> unparsed
    ("20 000–15 000 PLN", (15000.0, 20000.0, "PLN", "month", "", 15000.0, 20000.0)),  # swap
    ("15 000–20 000", (15000.0, 20000.0, "", "", "", None, None)),  # no currency
    ("15 000–20 000 PLN / tydzień", (15000.0, 20000.0, "PLN", "", "", None, None)),  # week
]


def _tuple(p: SalaryParse) -> tuple:
    return (p.min, p.max, p.currency, p.period, p.contract, p.monthly_min, p.monthly_max)


@pytest.mark.parametrize("raw, expected", CASES, ids=[repr(c[0]) for c in CASES])
def test_parse_salary_table(raw, expected):
    assert _tuple(parse_salary(raw)) == expected


def test_raw_is_stripped_input_or_empty_for_none():
    assert parse_salary(None).raw == ""
    assert parse_salary("  15 000 PLN  ").raw == "15 000 PLN"


@pytest.mark.parametrize(
    "raw, period, monthly",
    [
        ("500 PLN", "hour", 80000.0),  # < 1 000 -> hour
        ("5 000 PLN", "month", 5000.0),  # [1 000, 200 000) -> month
        ("250 000 PLN", "year", 20833.33),  # >= 200 000 -> year
    ],
)
def test_period_heuristic_bands_are_flagged_assumed(raw, period, monthly):
    p = parse_salary(raw)
    assert p.period == period
    assert p.period_assumed is True
    assert p.monthly_min == p.monthly_max == monthly


def test_heuristic_uses_the_larger_amount():
    # 900–1 200: the larger side lands in the month band, so the whole range is monthly.
    p = parse_salary("900–1 200 EUR")
    assert (p.period, p.period_assumed) == ("month", True)


@pytest.mark.parametrize(
    "raw",
    ["120–150 zł / godz.", "$80 000+ USD/yr", "€ 5 000 – 7 000 / month", "£450/day"],
)
def test_explicit_period_token_is_not_assumed(raw):
    assert parse_salary(raw).period_assumed is False


def test_parsed_requires_amount_and_currency():
    assert parse_salary("15 000–20 000 PLN").parsed is True
    assert parse_salary("15 000–20 000").parsed is False  # amount, no currency
    assert parse_salary("PLN").parsed is False  # currency, no amount
    assert parse_salary("Competitive").parsed is False
    assert parse_salary(None).parsed is False


def test_no_currency_means_no_heuristic_and_no_monthly():
    p = parse_salary("15 000–20 000")
    assert (p.period, p.period_assumed, p.monthly_min, p.monthly_max) == ("", False, None, None)


def test_week_token_blocks_heuristic_but_keeps_amounts():
    p = parse_salary("2 000 PLN / tydzień")
    assert p.parsed is True
    assert (p.period, p.period_assumed, p.monthly_min) == ("", False, None)


def test_absurd_amount_unparses_whole_string_but_keeps_facts():
    p = parse_salary("15 000 000 000 PLN B2B")
    assert (p.min, p.max, p.period, p.monthly_min) == (None, None, "", None)
    assert (p.currency, p.contract) == ("PLN", "b2b")
    assert p.parsed is False


def test_b2b_digit_is_not_an_amount():
    # The "2" in B2B must never be read as a salary figure.
    p = parse_salary("B2B")
    assert (p.min, p.max, p.contract) == (None, None, "b2b")


def test_nbsp_and_narrow_nbsp_thousands_separators():
    assert parse_salary("20 000–26 000 PLN").min == 20000.0
    assert parse_salary("20 000 PLN").max == 20000.0


def test_never_raises_on_garbage():
    for junk in ["\x00\x01", "–––", "$", "k", "..", "?-?", "1e309 PLN", "∞ PLN", 12345, 3.5]:
        p = parse_salary(junk)  # type: ignore[arg-type]
        assert isinstance(p, SalaryParse)


def test_result_is_frozen():
    p = parse_salary("15 000 PLN")
    with pytest.raises(AttributeError):
        p.min = 1.0  # type: ignore[misc]
