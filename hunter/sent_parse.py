"""
Parse the free-text ``Sent`` column into a clean application date.

The ``Sent`` column does double duty: it holds either an *application date* (in many
inconsistent formats) or a free-text *reason / status note* ("выгасла", "повторка",
"EXPIRED", "не тот стек", "—", ...). This module turns a Sent cell into a real
``date`` when it represents an application, or ``None`` otherwise.

Used by:
  - ``tools/normalize_sent.py``     (writes the clean date into a new Sheets column)
  - ``tools/stats_sheet.py``        (read-only statistics)
  - any scheduled normalizer callback
"""

import re
from datetime import date

# Default year for dates written without one ("15 05", "1305"): the running year.
_DEFAULT_YEAR = date.today().year

# Substrings (lowercased) that mark a row as "expired / no longer available".
_EXPIRED_MARKERS = (
    "expired", "выгасла", "wygas", "no longer accepting", "inactive",
    "zakończył", "nie jest już dostęp", "nie została odnaleziona",
    "didn't find", "couldn't find", "bad gateway", "not there",
)

# Three-letter English month names → number, for "Applied on May 16, 2026".
_EN_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _valid(y: int, m: int, d: int) -> date | None:
    """Return a date if (y, m, d) is a real calendar date, else None."""
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_sent_date(value: str) -> date | None:
    """
    Best-effort parse of an application date out of a Sent cell.

    Handles the formats actually seen in the sheet:
      2026-07-04 00:00:00 | 08 04 26 | 15 05 | 1305 | 22 05 (21 05)
      24.04.2026 (Polish "Zaaplikowano ... 24.04.2026") | Applied on May 16, 2026
    Returns None for reason notes, EXPIRED markers, dashes and blanks.
    """
    s = (value or "").strip()
    if not s:
        return None
    low = s.lower()

    # ISO: 2026-07-04 (optionally with a time component after it).
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return _valid(int(m.group(1)), int(m.group(2)), int(m.group(3)))

    # DD.MM.YYYY (Polish "Zaaplikowano na tę ofertę 24.04.2026").
    m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b", s)
    if m:
        return _valid(int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # "Applied on May 16, 2026" / "May 16 2026".
    m = re.search(r"\b([a-z]{3})[a-z]*\.?\s+(\d{1,2}),?\s+(\d{4})\b", low)
    if m and m.group(1) in _EN_MONTHS:
        return _valid(int(m.group(3)), _EN_MONTHS[m.group(1)], int(m.group(2)))

    # If we reach here and the cell carries an "expired"-style note, it's not a date.
    if any(mark in low for mark in _EXPIRED_MARKERS):
        return None

    # DD MM YY → "08 04 26" (two-digit year, assume 20xx).
    m = re.match(r"^(\d{1,2})[ .](\d{1,2})[ .](\d{2})\b", s)
    if m:
        return _valid(2000 + int(m.group(3)), int(m.group(2)), int(m.group(1)))

    # DD MM (no year) → "15 05", first pair of "22 05 (21 05)".
    m = re.match(r"^(\d{1,2})[ ./](\d{1,2})\b", s)
    if m:
        return _valid(_DEFAULT_YEAR, int(m.group(2)), int(m.group(1)))

    # DDMM compact → "1305" = 13 May.
    m = re.fullmatch(r"(\d{2})(\d{2})", s)
    if m:
        return _valid(_DEFAULT_YEAR, int(m.group(2)), int(m.group(1)))

    return None


def classify(value: str) -> str:
    """Bucket a Sent cell into: applied | expired | blank | other."""
    s = (value or "").strip()
    if not s or s in {"-", "—", "–", "- ", " - "}:
        return "blank"
    if parse_sent_date(s) is not None:
        return "applied"
    if any(mark in s.lower() for mark in _EXPIRED_MARKERS):
        return "expired"
    return "other"
