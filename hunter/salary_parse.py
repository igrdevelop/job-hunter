"""
Deterministic salary-string parser ($0, no LLM, no I/O, stdlib only).

docs/MARKET_MEMORY_PLAN.md M1.b — turns the free-text ``Job.salary`` string the
24 sources produce into a comparable structure that will feed the
``postings_seen`` table. Reports only: nothing in the hunt or the apply
pipeline acts on the value, so a wrong parse costs nothing but a wrong
number in a digest.

Contract
--------
``parse_salary(raw)`` NEVER raises. Anything it cannot read comes back as
``''`` / ``None`` — never a guess — with exactly ONE documented exception:

**The period heuristic.** The Polish JSON boards (JustJoin, NoFluffJobs,
SmartJobs, Inhire, Bulldogjob) emit MONTHLY figures with no unit at all
(``"15 000–20 000 PLN B2B"``), and without a period the monthly
normalisation would be ``None`` for the bulk of the corpus. So when NO
period token is present but an amount AND a currency are, the period is
inferred from the magnitude of the larger amount:

    < 1 000              -> hour
    [1 000, 200 000)     -> month
    >= 200 000           -> year

and ``period_assumed`` is set to ``True`` so a consumer can tell an
inferred period from a stated one. Known limitation, accepted by design:
a yearly USD/EUR figure written without a unit (``"$100k - $150k"``,
``"€60k-80k"``) lands in the monthly band. The remote boards that emit
yearly pay (RemoteOK, Himalayas) always append ``/yr``, so this bites only
free-text sources (Remotive, Working Nomads). A ``week`` token (``/wk``,
``tydzień``) is recognised as a stated-but-unsupported period: it BLOCKS the
heuristic (period stays ``''``, ``monthly_*`` stay ``None``) rather than
mislabelling a weekly rate as monthly.

Other decisions worth knowing before "fixing" them:

* A bare single figure (``"$50k"``, ``"90 PLN/h"``) is a point value:
  ``min == max``. ``X+`` / ``od X`` / ``from X`` give min only; ``do X`` /
  ``up to X`` / ``max X`` give max only. theprotocol/pracuj/inhire write an
  open bound as ``?`` (``"?-20000 PLN"``, ``"15000-? PLN"``) — handled the
  same way.
* ``"PLN 18–24k"`` / ``"20-26k zl"``: a ``k`` suffix on one side of a range
  is propagated to the other side when that side is < 1 000 without it.
* More than two amounts: the first two are the range, the rest are ignored.
* ``min > max`` is swapped. Any amount > 10 000 000, an amount of 0, or a
  leading minus sign makes the whole string unparsed (amounts ``None``;
  currency/contract are still reported since they are plain facts).
* The bare word ``contract`` is NOT a contract token (``"employment
  contract"`` -> uop and ``contractor`` -> other are). SmartJobs' ``UoD``
  and ``Internship`` labels map to ``other``.
* ``monthly_*`` = hour x 160, day x 20, year / 12, month as-is, rounded to
  2 decimals, in the string's own currency — no FX conversion here.

Keep this module importable standalone: no ``hunter.config`` import, no
network, no filesystem.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["SalaryParse", "parse_salary"]

MAX_SANE_AMOUNT = 10_000_000.0
HOURS_PER_MONTH = 160
DAYS_PER_MONTH = 20

# Magnitude bands for the period heuristic (see the module docstring).
_HOUR_BAND_BELOW = 1_000.0
_YEAR_BAND_FROM = 200_000.0

_PL_LETTERS = "a-ząćęłńóśźż"
_NOT_LETTER_AFTER = rf"(?![{_PL_LETTERS}])"
_NOT_LETTER_BEFORE = rf"(?<![{_PL_LETTERS}])"

# One amount: "20 000", "20,000", "20.000", "20000", "20k", "20.5k", "89.3",
# "1,5k". A letter/digit directly before the first digit disqualifies it, so
# the "2" in "B2B" never becomes an amount. The k-suffix must not start a
# word ("20 kontrakt" is 20, not 20k).
_NUM_RE = re.compile(
    r"""
    (?<![A-Za-z0-9])
    (?P<int>
        \d{1,3}(?:[ \u00a0\u202f]\d{3})+(?!\d)   # 20 000 / 1 500 000
      | \d{1,3}(?:,\d{3})+(?!\d)                 # 20,000
      | \d{1,3}(?:\.\d{3})+(?!\d)                # 20.000 (EU dotted thousands)
      | \d+
    )
    (?:[.,](?P<dec>\d{1,2})(?!\d))?
    (?:\s?(?P<k>[kK])(?![A-Za-z]))?
    """,
    re.VERBOSE,
)

_NEGATIVE_RE = re.compile(r"^\s*[-−]\s*\d")
_PLUS_AFTER_RE = re.compile(r"^\+")
_QMARK_BEFORE_RE = re.compile(r"\?\s*[-–—]\s*$")
_QMARK_AFTER_RE = re.compile(r"^\s*[-–—]\s*\?")
_MAX_PREFIX_RE = re.compile(
    r"(?:^|[\s(])(?:do|up\s*to|max(?:imum)?\.?|maks(?:imum|\.)?|to|till|until)\s*[$€£]?\s*$",
    re.IGNORECASE,
)
_MIN_PREFIX_RE = re.compile(
    r"(?:^|[\s(])(?:od|from|min(?:imum)?\.?|at\s+least|starting\s+(?:at|from))\s*[$€£]?\s*$",
    re.IGNORECASE,
)

# Currency tokens, scanned by position — the earliest wins.
_CURRENCY_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "PLN",
        re.compile(
            rf"{_NOT_LETTER_BEFORE}(?:pln|z[łl](?:oty|otych|ote|ot)?){_NOT_LETTER_AFTER}",
            re.IGNORECASE,
        ),
    ),
    ("EUR", re.compile(rf"€|{_NOT_LETTER_BEFORE}eur(?:o)?{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("USD", re.compile(rf"\$|{_NOT_LETTER_BEFORE}usd{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("GBP", re.compile(rf"£|{_NOT_LETTER_BEFORE}gbp{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("CHF", re.compile(rf"{_NOT_LETTER_BEFORE}chf{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    # ISO codes the remote/global boards actually emit (M0 probe on prod,
    # 2026-09-14: every 4dayweek "…CAD/yr" range was unparsed). Plain
    # word-bounded codes only — "$" is already USD above, and a "C$"/"A$"
    # prefix is rare enough in the corpus not to be worth its own rule.
    ("CAD", re.compile(rf"{_NOT_LETTER_BEFORE}cad{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("AUD", re.compile(rf"{_NOT_LETTER_BEFORE}aud{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("SEK", re.compile(rf"{_NOT_LETTER_BEFORE}sek{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("NOK", re.compile(rf"{_NOT_LETTER_BEFORE}nok{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("DKK", re.compile(rf"{_NOT_LETTER_BEFORE}dkk{_NOT_LETTER_AFTER}", re.IGNORECASE)),
    ("CZK", re.compile(rf"{_NOT_LETTER_BEFORE}(?:czk|kč){_NOT_LETTER_AFTER}", re.IGNORECASE)),
)

# Period tokens, scanned by position — the earliest wins. "week" is an
# explicit UNKNOWN that suppresses the magnitude heuristic.
_PERIOD_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "hour",
        re.compile(
            rf"/\s*(?:h|hr|hrs|hour|hours)(?![A-Za-z])"
            rf"|{_NOT_LETTER_BEFORE}(?:per\s+hour|hourly|hours?|godz){_NOT_LETTER_AFTER}"
            rf"|{_NOT_LETTER_BEFORE}godz",
            re.IGNORECASE,
        ),
    ),
    (
        "day",
        re.compile(
            rf"/\s*(?:d|day|days|md)(?![A-Za-z])"
            rf"|{_NOT_LETTER_BEFORE}(?:per\s+day|daily|dzie[ńn]|dziennie|md){_NOT_LETTER_AFTER}",
            re.IGNORECASE,
        ),
    ),
    (
        "year",
        re.compile(
            rf"/\s*(?:y|yr|year|annum)(?![A-Za-z])"
            rf"|{_NOT_LETTER_BEFORE}(?:per\s+year|per\s+annum|yearly|year|annual(?:ly)?"
            rf"|p\.\s*a\.|rocznie){_NOT_LETTER_AFTER}",
            re.IGNORECASE,
        ),
    ),
    (
        "month",
        re.compile(
            rf"/\s*(?:m|mo|month)(?![A-Za-z])"
            rf"|{_NOT_LETTER_BEFORE}(?:per\s+month|monthly|month|mies){_NOT_LETTER_AFTER}"
            rf"|{_NOT_LETTER_BEFORE}mies",
            re.IGNORECASE,
        ),
    ),
    (
        "",  # week: stated, unsupported -> unknown, and NO heuristic
        re.compile(
            rf"/\s*(?:w|wk|week)(?![A-Za-z])"
            rf"|{_NOT_LETTER_BEFORE}(?:per\s+week|weekly|week|tydz|tygod)",
            re.IGNORECASE,
        ),
    ),
)

# Contract tokens, scanned by position — the earliest wins.
_CONTRACT_RES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("b2b", re.compile(r"(?<![A-Za-z0-9])b2b(?![A-Za-z0-9])", re.IGNORECASE)),
    (
        "uop",
        re.compile(
            rf"{_NOT_LETTER_BEFORE}(?:uop|umowa\s+o\s+prac[ęe]|employment\s+contract"
            rf"|permanent|etat){_NOT_LETTER_AFTER}",
            re.IGNORECASE,
        ),
    ),
    (
        "other",
        re.compile(
            rf"{_NOT_LETTER_BEFORE}(?:uz|uod|umowa[\s-]+zlecen(?:ie|ia|iu)?|umowa\s+o\s+dzie[łl]o"
            rf"|freelance|contractor|internship|sta[żz](?:u|ysta|ystka)?){_NOT_LETTER_AFTER}",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class SalaryParse:
    raw: str
    min: float | None
    max: float | None
    currency: str
    period: str
    period_assumed: bool
    contract: str
    monthly_min: float | None
    monthly_max: float | None

    @property
    def parsed(self) -> bool:
        return (self.min is not None or self.max is not None) and self.currency != ""


def _unparsed(raw: str, *, currency: str = "", contract: str = "") -> SalaryParse:
    return SalaryParse(raw, None, None, currency, "", False, contract, None, None)


def _first_token(text: str, table: tuple[tuple[str, re.Pattern[str]], ...]) -> str | None:
    """Earliest match across a token table; None when nothing matches."""
    best_pos: int | None = None
    best_label: str | None = None
    for label, rx in table:
        m = rx.search(text)
        if m and (best_pos is None or m.start() < best_pos):
            best_pos, best_label = m.start(), label
    return best_label


def _amount_value(m: re.Match[str]) -> float:
    digits = re.sub(r"[ \u00a0\u202f,.]", "", m.group("int"))
    value = float(digits)
    if m.group("dec"):
        value += float(f"0.{m.group('dec')}")
    if m.group("k"):
        value *= 1000
    return value


def _to_monthly(value: float | None, period: str) -> float | None:
    if value is None:
        return None
    if period == "hour":
        return round(value * HOURS_PER_MONTH, 2)
    if period == "day":
        return round(value * DAYS_PER_MONTH, 2)
    if period == "year":
        return round(value / 12, 2)
    if period == "month":
        return round(value, 2)
    return None


def _extract_bounds(text: str) -> tuple[float | None, float | None] | None:
    """(min, max) from the amounts in *text*; None when the string is unparsable."""
    if _NEGATIVE_RE.match(text):
        return None
    matches = list(_NUM_RE.finditer(text))
    if not matches:
        return (None, None)

    if len(matches) >= 2:
        lo_m, hi_m = matches[0], matches[1]
        lo, hi = _amount_value(lo_m), _amount_value(hi_m)
        # "PLN 18–24k" / "15k–20": one side's k-suffix propagates to the other.
        if hi_m.group("k") and not lo_m.group("k") and lo < 1000:
            lo *= 1000
        elif lo_m.group("k") and not hi_m.group("k") and hi < 1000:
            hi *= 1000
        if lo > hi:
            lo, hi = hi, lo
        if _sane(lo) and _sane(hi):
            return (lo, hi)
        return None

    m = matches[0]
    value = _amount_value(m)
    if not _sane(value):
        return None
    before, after = text[: m.start()], text[m.end() :]
    if _PLUS_AFTER_RE.match(after) or _QMARK_AFTER_RE.match(after):
        return (value, None)
    if _QMARK_BEFORE_RE.search(before) or _MAX_PREFIX_RE.search(before):
        return (None, value)
    if _MIN_PREFIX_RE.search(before):
        return (value, None)
    return (value, value)


def _sane(value: float) -> bool:
    return 0 < value <= MAX_SANE_AMOUNT


def parse_salary(raw: str | None) -> SalaryParse:
    """Parse a free-text salary string. Never raises; see the module docstring."""
    try:
        text = "" if raw is None else str(raw).strip()
        return _parse(text)
    except Exception:  # a parser over scraped text must never take a hunt down
        return _unparsed("" if raw is None else str(raw).strip())


def _parse(text: str) -> SalaryParse:
    if not text:
        return _unparsed(text)

    currency = _first_token(text, _CURRENCY_RES) or ""
    contract = _first_token(text, _CONTRACT_RES) or ""

    bounds = _extract_bounds(text)
    if bounds is None:
        return _unparsed(text, currency=currency, contract=contract)
    lo, hi = bounds
    if lo is None and hi is None:
        return _unparsed(text, currency=currency, contract=contract)

    period_token = _first_token(text, _PERIOD_RES)  # None = absent, '' = week (unknown)
    period_assumed = False
    if period_token is not None:
        period = period_token
    elif currency:
        larger = max(v for v in (lo, hi) if v is not None)
        if larger < _HOUR_BAND_BELOW:
            period = "hour"
        elif larger < _YEAR_BAND_FROM:
            period = "month"
        else:
            period = "year"
        period_assumed = True
    else:
        period = ""

    return SalaryParse(
        raw=text,
        min=lo,
        max=hi,
        currency=currency,
        period=period,
        period_assumed=period_assumed,
        contract=contract,
        monthly_min=_to_monthly(lo, period),
        monthly_max=_to_monthly(hi, period),
    )
