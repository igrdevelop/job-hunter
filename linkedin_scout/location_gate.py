"""Vendored, simplified copy of the anti-hybrid-city location gate.

Phase 0 of docs/SCOUT_REPO_SPLIT_PLAN.md §4.2: the scout previously imported
`hunter.filters._is_unwanted_onsite_location` + `hunter.models.Job` (building
a throwaway Job just to call one function). That import is the last hard
dependency on `hunter` this package has — vendoring it here lets the scout
move into its own repo as a pure file copy.

**Why vendoring is safe / why drift doesn't matter:** this gate is only a
noise filter on the scout side. Every relayed candidate still goes through
the bot's own central filters (`hunter/filters.py`) and the doomed-vacancy
gate — those remain the SINGLE authoritative screen for what actually reaches
generation. A stricter vendored copy here is fine (worse: a few genuine posts
never get relayed); a looser one is also fine, just noisier (worse: a few
more posts get relayed that the bot-side gates then reject for free) — either
way nothing wrong ever reaches CV generation because of this file. That is
why simplification and copy-drift are both acceptable here in a way they
would not be for the bot-side filters themselves.

Provenance: ported from `hunter/filters.py::_is_unwanted_onsite_location`
(+ `_onsite_signal_positions`, `_is_acceptable_weekly_hybrid`,
`_ANTI_HYBRID_CITIES` incl. `config.py`'s `extra_anti_hybrid_cities`) as of
2026-07-08. Re-sync opportunistically, not automatically — this module
operates on plain text (no `Job` object, no `FILTER` config dict, no
config-flag gating — the checks below are unconditionally active since the
scout has no equivalent of `exclude_body_onsite_city`/
`allow_weekly_hybrid_warsaw_krakow` toggles).

Ported: the anti-hybrid city list (incl. the non-Polish cities from
`extra_anti_hybrid_cities`), the fully-remote veto regexes, the ~120-char
onsite-signal/city proximity window, the perks-context veto ("onsite
dining"-style false positives), the Wrocław veto, and the ~1-day/week
Warsaw/Kraków hybrid acceptance (it ported cleanly to plain text — no
`Job.title`/`Job.location` split was needed, since the scout only ever has
one blob of post text to check).

NOT ported: `_matches_location`'s title/location-field checks (the scout has
no separate title/location fields — it's a single wall of post text), and the
relocation-required / German-language / body-disqualifier rules (out of scope
for a location-only gate).
"""

from __future__ import annotations

import re

# Cities where hybrid work is NOT acceptable (too far from Wrocław).
# Mirrors hunter/filters.py::_ANTI_HYBRID_CITIES merged with
# hunter/config.py's FILTER["extra_anti_hybrid_cities"] as of 2026-07-08.
_ANTI_HYBRID_CITIES: frozenset[str] = frozenset(
    {
        "kraków", "krakow", "cracow",
        "warszawa", "warsaw",
        "gdańsk", "gdansk", "gdynia", "trójmiasto", "trojmiasto",
        "poznań", "poznan",
        "łódź", "lodz",
        "katowice", "silesia", "śląsk", "slask",
        "rzeszów", "rzeszow",
        "lublin",
        "szczecin",
        "bydgoszcz",
        "toruń", "torun",
        "białystok", "bialystok",
        # Non-Polish cities (config.py extra_anti_hybrid_cities)
        "helsinki", "helsingfors",
        "barcelona", "madrid", "lisbon", "lisboa",
        "berlin", "munich", "münchen", "hamburg", "frankfurt",
        "amsterdam", "rotterdam",
        "prague", "brno",
        "bratislava",
        "budapest",
        "bucharest",
        "sofia",
        "zagreb",
        "limassol", "nicosia", "larnaca", "larnaka", "paphos", "pafos",
        "islamabad", "karachi", "lahore",
        "bangalore", "mumbai", "delhi",
        "singapore",
        "dubai", "abu dhabi",
        "hong kong",
        "tokyo",
    }
)

# On-site / hybrid signals (English + Polish) that, when sitting next to an
# anti-hybrid city, mean the role is not remote-from-Wrocław.
_ONSITE_SIGNAL_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhybrid\b",
        r"\bon[-\s]?site\b",
        r"\bonsite\b",
        r"\bstationary\b",
        r"\bstacjonarn\w*",
        r"\bin[-\s]the[-\s]office\b",
        r"\bin[-\s]office\b",
        r"\bdays?\s+(?:a|per)\s+week\b",
        r"\bdays?\s+in\s+the\s+office\b",
        r"\bz\s+biura\b",
        r"\bw\s+biurze\b",
    )
)

# Perks/benefits-list noise that reuses "on-site"/"onsite" for something that
# has nothing to do with where the candidate must work (free onsite lunch, a
# gym on the premises …). See hunter/filters.py::_ONSITE_PERKS_CONTEXT_RE for
# the real-posting example that motivated this.
_ONSITE_PERKS_CONTEXT_RE = re.compile(
    r"\b(?:dining|lunch(?:es)?|snacks?|cafeteria|canteen|coffee|breakfast|"
    r"free\s+food|kitchen|parking|bike\s+storage|gym(?:\s+membership)?|"
    r"office\s+dog|perks?\s+and\s+rewards?)\b",
    re.IGNORECASE,
)


def _onsite_signal_positions(blob: str) -> list[int]:
    """Match starts of _ONSITE_SIGNAL_RES, minus perks-bullet noise."""
    return [
        m.start()
        for p in _ONSITE_SIGNAL_RES
        for m in p.finditer(blob)
        if not _ONSITE_PERKS_CONTEXT_RE.search(blob[m.start(): m.start() + 100])
    ]


# Strong fully-remote signals — if present, do NOT block on a body city mention.
_FULLY_REMOTE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfully\s+remote\b",
        r"\b100\s*%\s*remote\b",
        r"\bremote[-\s]first\b",
        r"\bwork\s+from\s+anywhere\b",
        r"\bw\s+pełni\s+zdaln\w*",
        r"\b100\s*%\s*zdaln\w*",
        r"tryb\s+pracy:?\s*\n?\s*\[?\s*(?:100\s*%\s*)?zdaln\w*",
    )
)

# Cities for which a ~1-day/week hybrid is acceptable (commutable from Wrocław).
_WEEKLY_HYBRID_CITIES: frozenset[str] = frozenset(
    {"warszawa", "warsaw", "kraków", "krakow", "cracow"}
)

# Low-frequency hybrid phrasing (≈ once a week) — English + Polish.
_WEEKLY_HYBRID_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bonce\s+(?:a|per)\s+week\b",
        r"\bone\s+day\s+(?:a|per|in\s+the)\s+week\b",
        r"\b1\s*(?:day|dni|dzień)\s*(?:a|per|/|in|w)\s*(?:week|tydz)\w*",
        r"\b1\s*x\s*(?:/|\s)*(?:a\s+)?(?:week|tydz\w*|wk)\b",
        r"\braz\s+w\s+tygodniu\b",
        r"\bjeden\s+dzień\s+w\s+tygodniu\b",
    )
)


def _is_acceptable_weekly_hybrid(blob: str) -> bool:
    """True → keep despite a Warsaw/Kraków hybrid, because it is only ~1 day/week.

    Ported from hunter/filters.py::_is_acceptable_weekly_hybrid; unconditionally
    active here (the bot's `allow_weekly_hybrid_warsaw_krakow` config flag has
    no scout-side equivalent — this is a noise filter, not a policy gate).
    """
    if not any(c in blob for c in _WEEKLY_HYBRID_CITIES):
        return False
    other_cities = _ANTI_HYBRID_CITIES - _WEEKLY_HYBRID_CITIES
    if any(c in blob for c in other_cities):
        return False
    return any(p.search(blob) for p in _WEEKLY_HYBRID_RES)


def is_unwanted_onsite_location(text: str) -> bool:
    """True → reject (text couples an on-site/hybrid signal with a far-away city).

    Ported from hunter/filters.py::_is_unwanted_onsite_location, operating on
    plain text instead of a Job object. Requires the on-site signal and the
    city to sit within a short window of each other to avoid false positives
    on posts that merely mention a head-office city in passing. A strong
    fully-remote signal, a Wrocław mention, or an acceptable ~1-day/week
    Warsaw/Kraków hybrid vetoes it.
    """
    blob = text.lower()
    if not blob.strip():
        return False
    if "wroc" in blob:
        return False
    if any(p.search(blob) for p in _FULLY_REMOTE_RES):
        return False
    onsite_pos = _onsite_signal_positions(blob)
    if not onsite_pos:
        return False
    city_pos: list[int] = []
    for city in _ANTI_HYBRID_CITIES:
        idx = blob.find(city)
        while idx != -1:
            city_pos.append(idx)
            idx = blob.find(city, idx + 1)
    if not city_pos:
        return False
    if not any(abs(o - c) <= 120 for o in onsite_pos for c in city_pos):
        return False
    return not _is_acceptable_weekly_hybrid(blob)
