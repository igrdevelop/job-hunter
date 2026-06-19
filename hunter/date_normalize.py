"""Normalize free-form period strings to MM/YYYY – MM/YYYY (or Present).

The LLM writes role periods in whatever shape it picked up from the candidate
profile ("Jan 2020 – Mar 2024", "August 2020 – Present", "2020 – 2024"). Taleo
and a couple of legacy ATS parsers expect strict MM/YYYY dates and silently
drop roles whose period header doesn't match. Workday + Greenhouse accept
either form but score MM/YYYY higher.

This module enforces the strict form at render time — we treat the period as
a pure rendering concern so the LLM doesn't need to know about it (the prompt
already has enough rules).

Conservative rules:
- Year-only ("2020 – 2024") is LEFT UNCHANGED. We don't fabricate months.
- "Present" / "Current" / "Now" / Polish "Obecnie" are kept as-is on the
  right side and joined with " – ".
- Polish month names are recognised (the same render path is reused for the
  PL CV).
- Anything we can't confidently parse is returned unchanged — the original
  string still renders and the ATS gets whatever the LLM wrote.
"""

from __future__ import annotations

import re

_DASH_RE = re.compile(r"\s*[–—-]\s*")

# Lower-case month-name → MM lookup (English + Polish, full + short forms).
_MONTHS: dict[str, str] = {
    # English full
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    # English short
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "sept": "09", "oct": "10", "nov": "11", "dec": "12",
    # Polish full
    "styczeń": "01", "luty": "02", "marzec": "03", "kwiecień": "04",
    "maj": "05", "czerwiec": "06", "lipiec": "07", "sierpień": "08",
    "wrzesień": "09", "październik": "10", "listopad": "11", "grudzień": "12",
    # Polish genitive (used in real PL date phrasing: "marca 2024")
    "stycznia": "01", "lutego": "02", "marca": "03", "kwietnia": "04",
    "maja": "05", "czerwca": "06", "lipca": "07", "sierpnia": "08",
    "września": "09", "października": "10", "listopada": "11", "grudnia": "12",
    # Polish short (3-letter, lowercase)
    "sty": "01", "lut": "02", "kwi": "04",
    "cze": "06", "lip": "07", "sie": "08", "wrz": "09", "paź": "10",
    "lis": "11", "gru": "12",
}

_PRESENT_RE = re.compile(
    r"^(present|current|now|today|obecnie|teraz|nadal)$",
    re.IGNORECASE,
)


def _try_one(side: str) -> str | None:
    """Parse one side of the period (start or end) into MM/YYYY or 'Present'.

    Returns None if we can't confidently parse it — caller will keep the
    original string unchanged.
    """
    s = side.strip().rstrip(".,")
    if not s:
        return None
    if _PRESENT_RE.match(s):
        return "Present"

    # Already MM/YYYY
    m = re.fullmatch(r"(\d{1,2})/(\d{4})", s)
    if m:
        mm, yyyy = m.group(1), m.group(2)
        return f"{int(mm):02d}/{yyyy}"

    # "Mar 2024", "March 2024", "Marca 2024", "Sty 2024" — month then year
    m = re.fullmatch(r"([A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż]+)\.?\s+(\d{4})", s)
    if m:
        mon_key = m.group(1).lower()
        if mon_key in _MONTHS:
            return f"{_MONTHS[mon_key]}/{m.group(2)}"
        return None

    # "2024 Mar" — year then month (rare but seen)
    m = re.fullmatch(r"(\d{4})\s+([A-Za-zĄąĆćĘęŁłŃńÓóŚśŹźŻż]+)\.?", s)
    if m:
        mon_key = m.group(2).lower()
        if mon_key in _MONTHS:
            return f"{_MONTHS[mon_key]}/{m.group(1)}"
        return None

    # Year-only "2024" → leave as YYYY (don't fabricate a month).
    if re.fullmatch(r"\d{4}", s):
        return s

    return None


def normalize_period(period: str) -> str:
    """Normalize a period like 'Jan 2020 – Mar 2024' to '01/2020 – 03/2024'.

    Returns the original string unchanged if either side can't be parsed —
    rendering a possibly-imperfect period is always safer than substituting
    something subtly wrong.
    """
    if not isinstance(period, str) or not period.strip():
        return period or ""

    parts = _DASH_RE.split(period.strip(), maxsplit=1)
    if len(parts) == 2:
        left = _try_one(parts[0])
        right = _try_one(parts[1])
        if left is None or right is None:
            return period
        return f"{left} – {right}"

    # No dash — single date (e.g. "Mar 2024", or a date string that's just one)
    single = _try_one(period.strip())
    return single if single is not None else period
