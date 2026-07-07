"""Pure hiring-post heuristic + location gate for LinkedIn content-search posts.

No Playwright import here and no module-level side effects — this is the part of
the scout that must be unit-testable without a browser (M1, see
docs/LINKEDIN_POSTS_SCOUT_TASK.md). Ported from docs/LINKEDIN_POSTS_SOURCE_PLAN.md
§4.2 (branch feat/linkedin-posts-source) plus the live-probe negatives from that
plan's §4.6 round 2 (US-staffing noise, Angular-prominence gate, szukam/szukamy).

The location gate reuses hunter.filters._is_unwanted_onsite_location (the
anti-hybrid city list + regexes) instead of duplicating it, per the task spec.
"""

from __future__ import annotations

import re
from enum import Enum

from hunter.filters import _is_unwanted_onsite_location
from hunter.models import Job

# --- Stack keyword -----------------------------------------------------------

STACK_KEYWORD_RE = re.compile(r"\bangular\b", re.IGNORECASE)

# A stronger form that also counts as "prominent" even outside the first 200
# chars — "Angular Developer", "Angular Engineer", "Angular Frontend" read as a
# role title, not a stack-dump mention.
_ANGULAR_ROLE_RE = re.compile(
    r"\bangular\s+(?:developer|engineer|frontend)\b", re.IGNORECASE
)

_ANGULAR_PROMINENCE_WINDOW = 200


def _is_angular_prominent(text: str) -> bool:
    """Angular must lead the post or read as a role, not sit inside a stack dump.

    Live calibration (plan §4.6 round 2): 'angular hiring' sorted-by-date is
    dominated by US staffing-mill posts that mention Angular only inside a long
    stack dump (Java/.NET fullstack). Requiring prominence — either near the top
    of the post or phrased as an explicit role — filters most of that noise.
    """
    head = text[:_ANGULAR_PROMINENCE_WINDOW]
    if STACK_KEYWORD_RE.search(head):
        return True
    return bool(_ANGULAR_ROLE_RE.search(text))


# --- Hiring signal (EN + PL) ---------------------------------------------------

HIRING_SIGNAL_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhiring\b",
        r"we[’']?re\s+looking\s+for",
        r"\blooking\s+for\s+a\b",
        r"\bopen\s+role\b",
        r"\bopen\s+position\b",
        r"\bvacanc\w*\b",
        r"\bjoin\s+(?:our|the)\s+team\b",
        r"#hiring\b",
        r"#rekrutacja\b",
        r"\bszukamy\b",       # PL plural "we are looking" — hiring
        r"\bposzukujemy\b",
        r"\bzatrudnimy\b",
        r"\bpraca\s+dla\b",
        r"\bищем\b",          # RU "we are looking (for)" — hiring
        r"\bтребуется\b",
        r"\bвакансия\b",
        r"\bнабираем\b",
        r"#вакансия\b",
    )
)

# --- Candidate-side negatives (people announcing THEY seek work) --------------

CANDIDATE_SIDE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bopen\s+to\s+work\b",
        r"\blooking\s+for\s+(?:a\s+)?new\s+(?:opportunity|role)\b",
        # \bszukam\b (singular) intentionally does NOT match "szukamy" (plural,
        # hiring) — the word boundary after "m" fails inside "szukamy" because
        # "y" is a word character. This distinction is load-bearing (plan §4.6
        # round 2 live finding) — do not "fix" it into a looser stem match.
        r"\bszukam\b\s+pracy",
        r"#opentowork\b",
        r"\bищу\s+работу\b",   # RU "I'm looking for work" — candidate-side
        r"\bв\s+поиске\s+работы\b",
        r"#ищу_?работу\b",
    )
)

# --- Course / ad spam negatives -------------------------------------------------

SPAM_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bcourse\b",
        r"\bwebinar\b",
        r"\bbootcamp\b",
        r"\bszkolenie\b",
        r"\bkurs\w*\b",
        r"\bкурс\w*\b",
        r"\bвебинар\b",
        r"\bбуткемп\b",
    )
)

# --- US-staffing negatives (plan §4.6 round 2 live finding) --------------------

_US_STATE_CODES = (
    "VA", "NJ", "NY", "TX", "SC", "CA", "GA", "FL", "IL",
)

US_STAFFING_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\b(?:w2|c2c|h1b|usc|gc|green\s+card)\b",
        r"\bon-?site\b.{0,60}\b(?:" + "|".join(_US_STATE_CODES) + r")\b",
    )
)


def is_hiring_post(text: str) -> bool:
    """True → the post reads like a genuine Angular hiring post worth a card.

    Order matters: cheap disqualifiers first (no stack keyword at all), then the
    Angular-prominence gate, then the negative lists, and only then the positive
    hiring-signal requirement.
    """
    if not text:
        return False
    if not STACK_KEYWORD_RE.search(text):
        return False
    if not _is_angular_prominent(text):
        return False
    if any(p.search(text) for p in CANDIDATE_SIDE_RES):
        return False
    if any(p.search(text) for p in SPAM_RES):
        return False
    if any(p.search(text) for p in US_STAFFING_RES):
        return False
    return any(p.search(text) for p in HIRING_SIGNAL_RES)


# --- Location three-way gate ----------------------------------------------------


class LocationVerdict(Enum):
    """Outcome of the three-way location gate (task spec §3.2 / plan §4.2)."""

    KEEP = "keep"
    REJECT = "reject"


_REMOTE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bremote\b",
        r"\bfully\s+remote\b",
        r"\bzdalnie\b",
        r"\bpraca\s+zdalna\b",
        r"\bzdaln\w*",
        r"\bудал[её]нн?\w*",   # RU "remote(ly)" — удалённо/удаленка/удаленный
        r"\bдистанцион\w*",   # RU "distance/remote work"
    )
)

_WROCLAW_RE = re.compile(r"wroc\w*", re.IGNORECASE)


def check_location(text: str) -> LocationVerdict:
    """Three-way location gate, applied to the raw post text.

    1. Explicit remote/Wrocław mention anywhere in the post -> KEEP.
    2. Explicit on-site/hybrid signal tied to a non-Wrocław city (reusing
       hunter.filters._is_unwanted_onsite_location, not reimplemented) -> REJECT.
    3. No location info at all -> KEEP (the human decides at the Telegram card —
       this is the normal case for recruiter posts).
    """
    if not text:
        return LocationVerdict.KEEP
    if _WROCLAW_RE.search(text) or any(p.search(text) for p in _REMOTE_RES):
        return LocationVerdict.KEEP
    job = Job(
        title="",
        company="",
        location="",
        salary=None,
        url="",
        source="linkedin_posts",
        raw={"description": text},
    )
    if _is_unwanted_onsite_location(job):
        return LocationVerdict.REJECT
    return LocationVerdict.KEEP
