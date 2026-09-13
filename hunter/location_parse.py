"""Deterministic ($0, no LLM) location-string classifier.

docs/MARKET_MEMORY_PLAN.md M1.b: turn a source's free-text ``location`` field
("<City> (Hybrid)", "Remote — Poland, Germany", "w <City-locative>") into a
``LocationParse`` — a remote/hybrid/onsite mode plus a canonical city — for
the future ``postings_seen`` table. Reports only: nothing in the hunt or the
apply pipeline acts on the value.

Vocabulary is REUSED, never defined here:

* mode words for "no geographic restriction" come from
  ``hunter.sources.text_utils.REMOTE_ANY`` (plus a small private set of
  remote/hybrid/onsite MODE tokens — words, not places);
* cities come from ``hunter.filters._anti_hybrid_cities(flt)`` (the
  per-profile PL set + the profile's ``extra_anti_hybrid_cities``) and from
  the candidate's own home-city aliases via
  ``hunter.filter_profile._home_city_aliases()`` (``candidate.yaml``
  ``location.home_city`` / ``location.home_city_aliases``).

This module contains no city literal of its own — a second person running the
bot with a different ``candidate.yaml`` gets THEIR home city flagged, and the
readiness gate (tests/test_handoff_readiness.py) stays clean.

Matching is case- and diacritics-insensitive (``Lodz`` == ``Łódź``, via
``hunter.tracker._strip_diacritics``) and on word boundaries, so "Remoteleaf"
does not read as remote while a Polish locative ("w <City>ie") does match the
declined form the filter set already lists.

Canonicalisation caveat: ``_PL_ANTI_HYBRID_CITIES`` lists Polish declensions
(locative/genitive forms) next to the nominatives but carries NO declension →
nominative mapping, and none is invented here — ``city`` is the matched
vocabulary entry, lowercased and diacritics-folded (a locative input yields
the locative entry). A future mapping belongs in filters.py, next to the set.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from hunter.filter_profile import _home_city_aliases
from hunter.filters import _anti_hybrid_cities, _resolve_flt
from hunter.sources.text_utils import REMOTE_ANY
from hunter.tracker import _strip_diacritics

log = logging.getLogger(__name__)

REMOTE = "remote"
HYBRID = "hybrid"
ONSITE = "onsite"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class LocationParse:
    raw: str
    remote_mode: str
    city: str
    is_home_city: bool


_EMPTY = LocationParse(raw="", remote_mode=UNKNOWN, city="", is_home_city=False)


def _fold(text: str) -> str:
    """Lowercase + diacritics-folded + whitespace-collapsed form for matching."""
    folded = _strip_diacritics(text).lower()
    return re.sub(r"\s+", " ", folded).strip()


# ── Mode tokens (words only — NO places) ─────────────────────────────────────
# Written against the FOLDED input, so plain ASCII regexes suffice ("zdalną" →
# "zdalna"). Precedence is decided in classify_location: hybrid > onsite >
# remote — a "Hybrid, remote possible" listing is still a hybrid role.
_HYBRID_RE = re.compile(r"\b(?:hybrid\w*|hybryd\w*)\b")
_ONSITE_RE = re.compile(
    r"\b(?:on[-\s]?site|stacjonarn\w*|in[-\s]office|in\s+the\s+office|office[-\s]based)\b"
)
_REMOTE_TOKEN_RES = (
    r"remote",
    r"zdaln\w*",
    r"home\s+office",
    r"work\s+from\s+home",
    r"wfh",
)


def _phrase_regex(phrases: Iterable[str]) -> re.Pattern[str] | None:
    """One word-bounded alternation over folded phrases, longest first.

    Longest-first so that at the same position "zielona gora" wins over a
    shorter entry, and "anywhere in the world" over "anywhere". Internal
    spaces match any whitespace run.
    """
    folded = sorted({_fold(p) for p in phrases if p and _fold(p)}, key=len, reverse=True)
    if not folded:
        return None
    alts = "|".join(re.escape(p).replace(r"\ ", r"\s+") for p in folded)
    return re.compile(rf"(?<![a-z0-9])(?:{alts})(?![a-z0-9])")


_REMOTE_ANY_ALTS = tuple(
    re.escape(_fold(p)).replace(r"\ ", r"\s+") for p in sorted(REMOTE_ANY, key=len, reverse=True)
)
_REMOTE_RE = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(_REMOTE_TOKEN_RES + _REMOTE_ANY_ALTS) + r")(?![a-z0-9])"
)


# ── Per-profile city matcher, cached like filters._anti_hybrid_cities ────────
_CityKey = tuple[tuple[str, ...], tuple[str, ...]]
_city_cache: dict[_CityKey, tuple[re.Pattern[str] | None, frozenset[str]]] = {}


def _city_matcher(flt: Mapping[str, Any]) -> tuple[re.Pattern[str] | None, frozenset[str]]:
    """(combined city regex, folded home-alias set) for this profile.

    Keyed on the folded vocabularies themselves rather than on the profile
    object, so two profiles with the same cities share one compiled regex and
    a candidate.yaml swap in tests (``candidate._set_path``) is honoured.
    """
    home = frozenset(_fold(a) for a in _home_city_aliases() if _fold(a))
    anti = frozenset(_fold(c) for c in _anti_hybrid_cities(flt) if _fold(c))
    key: _CityKey = (tuple(sorted(anti)), tuple(sorted(home)))
    cached = _city_cache.get(key)
    if cached is not None:
        return cached
    result = (_phrase_regex(anti | home), home)
    _city_cache[key] = result
    return result


def _remote_mode(folded: str) -> str:
    if _HYBRID_RE.search(folded):
        return HYBRID
    if _ONSITE_RE.search(folded):
        return ONSITE
    if _REMOTE_RE.search(folded):
        return REMOTE
    return UNKNOWN


def classify_location(raw: str | None, *, flt: Mapping[str, Any] | None = None) -> LocationParse:
    """Classify a source's location string. Never raises.

    ``remote_mode``: hybrid > onsite > remote > unknown, by token presence.
    A bare city with no mode word is ``unknown`` — never guessed as onsite.
    ``city``: the leftmost match from home-city aliases ∪ the profile's
    anti-hybrid cities, lowercased + diacritics-folded (see module docstring
    on declensions). ``is_home_city``: that match is one of the candidate's
    own aliases.
    """
    try:
        text = "" if raw is None else str(raw).strip()
        if not text:
            return _EMPTY
        folded = _fold(text)
        mode = _remote_mode(folded)
        city_re, home = _city_matcher(_resolve_flt(flt))
        city = ""
        is_home = False
        if city_re is not None:
            m = city_re.search(folded)
            if m:
                city = re.sub(r"\s+", " ", m.group(0))
                is_home = city in home
        return LocationParse(raw=text, remote_mode=mode, city=city, is_home_city=is_home)
    except Exception as exc:  # pragma: no cover - defensive: a report must never raise
        log.warning("classify_location failed for %r: %s", raw, exc)
        return LocationParse(
            raw="" if raw is None else str(raw).strip(),
            remote_mode=UNKNOWN,
            city="",
            is_home_city=False,
        )
