import html
import re
from dataclasses import dataclass

from hunter.models import Job
from hunter.config import FILTER, active_tracks

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)

# If any pattern matches job text → treat as German required (skip job),
# unless a german_not_required pattern matches first.
_GERMAN_REQUIRED_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # P-9.1: Job title signals — "Frontend Developer with German", "(German)", etc.
        # These appear in the title itself when German is a hard requirement.
        r"\bwith\s+german\b",
        r"\(german\)",
        r"\bgerman\s+speaking\b",
        r"\bspeaking\s+german\b",
        # English
        r"\bfluent\s+in\s+german\b",
        r"\bnative(?:[-\s]+level)?\s+german\b",
        r"\bgerman\s+native\b",
        r"\bprofessional\s+proficiency\s+in\s+german\b",
        r"\bworking\s+knowledge\s+of\s+(?:the\s+)?german\s+language\b",
        r"\bgerman\s+language\s+(?:skills?|proficiency)\b",
        r"\bgerman\s+(?:is\s+)?(?:required|mandatory|essential|a\s+must)\b",
        r"\bknowledge\s+of\s+german\s+is\s+(?:essential|required|mandatory)\b",
        r"\b(?:c1|c2|b2|b1)[\s\-]*(?:\(\s*)?german\b",
        r"\bgerman\s*[\(:]?\s*(?:c1|c2|b2|b1)\b",
        r"\bgerman\s+and\s+english\s+are\s+both\s+(?:required|mandatory)\b",
        r"\bbusiness\s+german\b",
        # German phrases
        r"\bdeutschkenntnisse?\b",
        r"\bsehr\s+gute\s+deutschkenntnisse\b",
        r"\bverhandlungssicher(?:es)?\s+deutsch\b",
        r"\bdeutsch\s+(?:fließend|fließende)\b",
        r"\bzwingend\s+.*\bdeutsch\b",
        r"\bvoraussetzung\b.*\bdeutschkenntnis",
        # Polish boards
        r"\bjęzyk\s+niemiecki\b.*\b(?:wymagany|wymagane|konieczn|b2|c1|c2)\b",
        r"\b(?:wymagany|wymagana|wymagane)\s+język\s+niemieckiego?\b",
        r"\b(?:wymagana?|bardzo\s+dobra?)\s+znajomość\s+niemieckiego\b",
        r"\bniemiecki\s*[\(:]?\s*(?:b2|c1|c2)\b",
    )
)

_GERMAN_NOT_REQUIRED_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bno\s+german\s+required\b",
        r"\bgerman\s+not\s+required\b",
        r"\bnot\s+require(?:d)?\s+german\b",
        r"\bknowledge\s+of\s+german\s+is\s+not\s+required\b",
        r"\benglish\s+is\s+(?:the\s+)?(?:working|company|office)\s+language\b",
        r"\bworking\s+language\s*[:\s]+\s*english\b",
    )
)


def _matches_title_keywords(title: str) -> bool:
    t = title.lower()
    return any(kw in t for kw in FILTER["title_keywords"])


def _requires_angular_check(title: str) -> bool:
    """If require_angular is on, title MUST contain 'angular' (case-insensitive)."""
    if not FILTER.get("require_angular", False):
        return True
    return "angular" in title.lower()


def _is_excluded_level(title: str) -> bool:
    t = title.lower()
    return any(lvl in t for lvl in FILTER["exclude_levels"])


def _matches_exclude_pattern(title: str) -> bool:
    """Regex-based exclusion: \\bjava\\b matches 'Java' but NOT 'JavaScript'."""
    patterns = FILTER.get("exclude_patterns", [])
    return any(re.search(p, title, re.IGNORECASE) for p in patterns)


def _lower_text_fragment(val) -> str:
    """Turn API-ish values (str, list, dict) into one lowercased string for matching."""
    if val is None or val == "":
        return ""
    if isinstance(val, str):
        return val.lower()
    if isinstance(val, (list, tuple)):
        return " ".join(_lower_text_fragment(x) for x in val)
    if isinstance(val, dict):
        return _lower_text_fragment(val.get("name", val.get("value", "")))
    return str(val).lower()


def _append_technology_field(tech_texts: list[str], technology) -> None:
    """Normalize raw['technology']: str, dict, or list of str/dict (e.g. SolidJobs categories)."""
    if technology is None:
        return
    if isinstance(technology, str):
        tech_texts.append(technology.lower())
    elif isinstance(technology, dict):
        tech_texts.append(_lower_text_fragment(technology.get("name")))
    elif isinstance(technology, list):
        for item in technology:
            if isinstance(item, dict):
                tech_texts.append(_lower_text_fragment(item.get("name")))
            else:
                tech_texts.append(_lower_text_fragment(item))


def _is_node_only_title(title: str) -> bool:
    """Return True when the title signals a Node.js backend role with no FE signal.

    Catches "TypeScript/Node.js Developer", "Node.js Backend Engineer", etc.
    that aren't caught by \bbackend\b because the word 'backend' isn't in the title.
    Runs for ALL sources.

    Does NOT fire when the title also contains a front-end signal (angular,
    frontend, react, ui, spa, ux) — those are full-stack roles we want to see.
    """
    if not FILTER.get("exclude_react_without_angular", False):
        # Re-use the same enable flag; Node filtering is part of "FE-only" mode
        return False

    t = title.lower()
    # Front-end signals — don't block if any of these appear as whole words
    _FE_SIGNAL_RES = (
        r"\bangular\b",
        r"\bfrontend\b",
        r"\bfront-end\b",
        r"\breact\b",
        r"\bvue\b",
        r"\bui\b",  # "UI / Node.js Developer" — UI is FE
        r"\bux\b",
        r"\bspa\b",
    )
    if any(re.search(p, t, re.IGNORECASE) for p in _FE_SIGNAL_RES):
        return False

    # Node.js in title + absence of FE signals = backend/full-stack role
    node_patterns = (
        r"\bnode\.?js\b",
        r"\bnode\s+developer\b",
        r"\bnode\s+engineer\b",
    )
    return any(re.search(p, t, re.IGNORECASE) for p in node_patterns)


_FULLSTACK_RE = re.compile(r"\bfull[-\s]?stack\b", re.IGNORECASE)


def _is_unwanted_fullstack(job: Job) -> bool:
    """Return True when a 'Full Stack / Fullstack' role should be blocked.

    Policy (owner's preference):
      - "Fullstack Developer"            → no Angular        → True (blocked).
      - "Full Stack Node.js"             → no Angular        → True (blocked).
      - "Fullstack (Angular + Node.js)"  → Angular + Node    → False (kept) —
            Node/Nuxt are intentionally absent from fullstack_backend_stacks.
      - "Full-Stack Spring Boot + Angular" / "Fullstack (C# + Angular)" →
            Angular + heavy backend (Java/Spring/.NET/C#/Python…) → True (blocked).

    The heavy-backend pairing is checked in the title AND the full job body, so a
    title that hides the backend ("FullStack Developer with Angular", Java in body)
    is still caught.
    """
    title = job.title or ""
    if not _FULLSTACK_RE.search(title):
        return False

    # Fullstack without any Angular signal in the title → always block.
    if "angular" not in title.lower():
        return True

    # Angular present: block only when paired with a heavy backend stack.
    if not FILTER.get("exclude_fullstack_with_backend", False):
        return False
    stacks = FILTER.get("fullstack_backend_stacks", [])
    if not stacks:
        return False
    haystack = f"{title}\n{_job_plain_text_blob(job)}"
    return any(re.search(p, haystack, re.IGNORECASE) for p in stacks)


def _react_track_active() -> bool:
    """True when the candidate is also applying for React roles
    (docs/quality/09-multi-track-react.md, CANDIDATE_TRACKS / `/tracks`).

    Gates the React-only exclusion filters below to no-ops without deleting
    them — they keep working as classifiers/statistics, `--force` still
    bypasses either way, and the default track set (angular-only) makes this
    return False, so default behavior is unchanged bit-for-bit.
    """
    return "react" in active_tracks()


def _is_react_only_title(title: str) -> bool:
    """Return True when the job title signals React-only with no Angular involvement.

    Title-only check that runs for ALL sources (including gmail_*) before the
    more expensive raw-data check.  Catches "React Developer", "React Native
    Engineer", "Frontend (React)" etc. that slip through the Gmail bypass.

    Only triggers when 'angular' is absent from the title.
    """
    if not FILTER.get("exclude_react_without_angular", False):
        return False
    if _react_track_active():
        return False
    t = title.lower()
    if "angular" in t:
        return False
    # Plain React role in title — must have a role word to avoid false positives
    # on descriptions like "React + Angular Developer"
    react_title_patterns = (
        r"\breact\s+developer\b",
        r"\breact\s+engineer\b",
        r"\breact\s+native\b",
        r"\breact\.js\s+developer\b",
        r"\breact\.js\s+engineer\b",
        r"\bfrontend\s+(?:developer|engineer)\s*[\(\[\{]?\s*react\b",
        r"\bsoftware\s+engineer\s+react\b",
        r"(?:^|\s)react\s*(?:developer|engineer|programm)",
    )
    return any(re.search(p, t, re.IGNORECASE) for p in react_title_patterns)


def _is_react_without_angular(job: Job) -> bool:
    """Skip React-only jobs: check title AND raw skills/tech data from API."""
    if not FILTER.get("exclude_react_without_angular", False):
        return False
    if _react_track_active():
        return False

    title = job.title.lower()
    raw = job.raw or {}

    # Collect all tech-related text from raw API data
    tech_texts = [title]

    # JustJoin: raw["skills"] = [{"name": "React.js", ...}, ...]; name may be nested
    for skill in raw.get("skills") or []:
        if isinstance(skill, dict):
            tech_texts.append(_lower_text_fragment(skill.get("name")))
        else:
            tech_texts.append(_lower_text_fragment(skill))

    # 4dayweek.io API: stack + tools hold frameworks (React lives here; skills are often soft skills)
    for key in ("stack", "tools"):
        for item in raw.get(key) or []:
            if isinstance(item, dict):
                tech_texts.append(_lower_text_fragment(item.get("name")))
            else:
                tech_texts.append(_lower_text_fragment(item))

    # Himalayas: categories / parentCategories are string lists
    for c in raw.get("categories") or []:
        tech_texts.append(_lower_text_fragment(c))
    for c in raw.get("parentCategories") or []:
        tech_texts.append(_lower_text_fragment(c))

    # Remoteleaf card summary (plain text blurb)
    summ = raw.get("summary")
    if isinstance(summ, str) and summ.strip():
        tech_texts.append(summ.lower())

    # NoFluffJobs: raw["technology"] = str; SolidJobs: list[{"name": "IT"}, ...]
    _append_technology_field(tech_texts, raw.get("technology"))

    # theprotocol.it: raw["technologies"] = ["JavaScript", "Angular", ...]
    for t in raw.get("technologies") or []:
        tech_texts.append(_lower_text_fragment(t))

    # Arbeitnow: raw["tags"] = ["javascript", "react", ...]
    for tag in raw.get("tags") or []:
        tech_texts.append(_lower_text_fragment(tag))
    for tile in raw.get("tiles", {}).get("values", []) or []:
        if isinstance(tile, dict):
            tech_texts.append(_lower_text_fragment(tile.get("value")))
        else:
            tech_texts.append(_lower_text_fragment(tile))

    # NoFluffJobs: category str | dict | list
    cat = raw.get("category", "")
    if isinstance(cat, str):
        tech_texts.append(cat.lower())
    elif isinstance(cat, (list, tuple)):
        for c in cat:
            tech_texts.append(_lower_text_fragment(c))
    elif isinstance(cat, dict):
        tech_texts.append(_lower_text_fragment(cat.get("name")))

    combined = " ".join(tech_texts)
    has_react = bool(re.search(r"\breact\b", combined))
    has_angular = "angular" in combined
    return has_react and not has_angular


def _strip_html_fragment(s: str) -> str:
    t = _HTML_TAG_RE.sub(" ", s)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _job_plain_text_blob(job: Job, max_chars: int = 24_000) -> str:
    """Title, location, and long text fields from raw (for language / requirement checks)."""
    parts: list[str] = [job.title or "", job.location or ""]
    raw = job.raw or {}
    favored_keys = (
        "description",
        "jobDescription",
        "content",
        "body",
        "about",
        "requirements",
        "offer",
    )
    seen: set[int] = set()

    def add_chunk(text: str) -> None:
        tid = id(text)
        if tid in seen:
            return
        seen.add(tid)
        parts.append(text)

    for key in favored_keys:
        v = raw.get(key)
        if isinstance(v, str) and v.strip():
            add_chunk(_strip_html_fragment(v))

    for key, v in raw.items():
        if key in favored_keys or not isinstance(v, str) or len(v) < 350:
            continue
        add_chunk(_strip_html_fragment(v))

    blob = " ".join(parts)
    return blob if len(blob) <= max_chars else blob[:max_chars]


# "Nice to have" / optional-section markers (EN + PL). A language match sitting
# shortly after one of these is a bonus, not a requirement — real M4 calibration
# false positive (docs/DOOMED_GATE_PLAN.md): a real SENT theprotocol.it posting
# (DHCBusinessSolutions) listed "Nice to have — Optional, ... German language
# skills" under an explicit optional heading.
_OPTIONAL_CONTEXT_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bnice\s+to\s+have\b",
        r"\boptional\b",
        r"\bbonus\b",
        r"\ba\s+plus\b",
        r"\bmile\s+widzian\w*\b",
        r"\bdodatkow\w*\s+atut\w*\b",
    )
)


def _is_optional_context(blob: str, pos: int, window: int = 150) -> bool:
    """True if an explicit nice-to-have/optional marker sits shortly before pos."""
    return any(p.search(blob[max(0, pos - window) : pos]) for p in _OPTIONAL_CONTEXT_RES)


def _is_german_language_required(job: Job) -> bool:
    """True → skip job (German appears to be a hard requirement)."""
    if not FILTER.get("exclude_german_language_required", False):
        return False
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return False
    if any(p.search(blob) for p in _GERMAN_NOT_REQUIRED_RES):
        return False
    for p in _GERMAN_REQUIRED_RES:
        for m in p.finditer(blob):
            if not _is_optional_context(blob, m.start()):
                return True
    return False


# Cities where hybrid work is NOT acceptable (too far from Wrocław).
# A job whose location or title contains one of these AND doesn't contain an
# allowed location token (remote/wroclaw) is rejected.
# LinkedIn often returns "Poland" as location with the city in the title (e.g.
# "Jlabs Angular Dev Kraków - Zabłocie"), so we check BOTH location and title.
# Extra cities from FILTER["extra_anti_hybrid_cities"] (config.py) are merged in
# at module load time so the set is computed once and stays O(1) per lookup.
_ANTI_HYBRID_CITIES: frozenset[str] = frozenset(
    {
        "kraków",
        "krakow",
        "cracow",
        "warszawa",
        "warsaw",
        "gdańsk",
        "gdansk",
        "gdynia",
        "trójmiasto",
        "trojmiasto",
        "poznań",
        "poznan",
        "łódź",
        "lodz",
        "katowice",
        "silesia",
        "śląsk",
        "slask",
        "rzeszów",
        "rzeszow",
        "lublin",
        "szczecin",
        "bydgoszcz",
        "toruń",
        "torun",
        "białystok",
        "bialystok",
    }
    | {c.lower() for c in FILTER.get("extra_anti_hybrid_cities", [])}
)

# ── Contract / part-time patterns (checked against full job text blob) ────────
# Catches "part-time" buried in the description rather than the title.
_CONTRACT_EXCLUDED_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        # Part-time — English
        r"\bpart[-\s]?time\b",
        # Part-time — Polish (0.5 etatu, pół etatu)
        r"\bpół\s+etatu\b",
        r"\b0[.,]5\s*(?:etatu|etat|fte)\b",
        # Very short contracts — 1-month engagements
        r"\b(?:1|one)\s*-?\s*month\s+(?:contract|project|engagement|assignment)\b",
        r"\bcontract\s+(?:for\s+)?(?:1|one)\s+month\b",
        r"\bduration\s*:?\s*1\s+month\b",
        r"\b1\s*[–-]\s*month\s+(?:contract|project)\b",
    )
)

# ── Relocation-required patterns (checked against full job text blob) ─────────
# Catches offers where location field says "remote/Poland" but the body demands
# physical relocation to a city outside the Wrocław area.
_RELOCATION_REQUIRED_RES: tuple[re.Pattern, ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brelocation\s+(?:is\s+)?(?:required|necessary|mandatory|expected)\b",
        r"\bmust\s+(?:be\s+)?(?:willing\s+to\s+)?relocate\b",
        r"\bwilling\s+to\s+relocate\s+(?:is\s+a?\s*)?(?:required|must|necessary)\b",
        # Polish
        r"\brelokacja\s+(?:jest\s+)?wymagana\b",
        r"\bwymagana\s+relokacja\b",
    )
)


def _is_unacceptable_contract(job: Job) -> bool:
    """True → skip (part-time or very short contract detected in full job text).

    Catches cases where "part-time" appears in the description but not the title
    — title-only exclude_levels / exclude_patterns miss these.
    """
    if not FILTER.get("exclude_unacceptable_contract", False):
        return False
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return False
    return any(p.search(blob) for p in _CONTRACT_EXCLUDED_RES)


def _requires_relocation(job: Job) -> bool:
    """True → skip (job explicitly requires relocation).

    Catches offers that show location='remote' or 'Poland' in the listing field
    but state in the description that the candidate must relocate.
    Works in tandem with _ANTI_HYBRID_CITIES (which blocks city mentions in
    location/title); this catches the rarer explicit relocation-required phrasing.
    """
    if not FILTER.get("exclude_relocation_required", False):
        return False
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return False
    return any(p.search(blob) for p in _RELOCATION_REQUIRED_RES)


# ── Body disqualifiers (title looks like clean FE, body says otherwise) ───────
def _has_body_disqualifier(job: Job) -> bool:
    """True → skip (a body_exclude_patterns token found in the full job text).

    Catches roles whose title is a clean "Frontend Developer" but whose body
    reveals a backend/CMS/low-code stack (Blazor, Mendix, WordPress, …) the
    candidate doesn't want — these slip past the title-only exclude_patterns.
    """
    if not FILTER.get("exclude_body_disqualifiers", False):
        return False
    pats = FILTER.get("body_exclude_patterns", [])
    if not pats:
        return False
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return False
    return any(re.search(p, blob, re.IGNORECASE) for p in pats)


# On-site / hybrid signals (English + Polish) that, when sitting next to an
# anti-hybrid city in the body, mean the role is not remote-from-Wrocław.
_ONSITE_SIGNAL_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bhybrid\b",
        r"\bon[-\s]?site\b",
        r"\bonsite\b",
        r"\bstationary\b",
        r"\bstacjonarn\w*",  # PL: praca stacjonarna
        r"\bin[-\s]the[-\s]office\b",
        r"\bin[-\s]office\b",
        r"\bdays?\s+(?:a|per)\s+week\b",  # "3 days a week" (in office)
        r"\bdays?\s+in\s+the\s+office\b",
        r"\bz\s+biura\b",  # PL: from the office
        r"\bw\s+biurze\b",  # PL: in the office
    )
)

# Perks/benefits-list noise that reuses "on-site"/"onsite" for something that has
# nothing to do with where the CANDIDATE must work (free onsite lunch, a gym on
# the premises, an office dog …). Real example from calibration (docs/DOOMED_GATE_
# PLAN.md M4, real SENT+relocated BitPanda posting): "Fuel and focus on-site –
# Pandas in Vienna, Bucharest, Barcelona, and Berlin can enjoy free onsite dining"
# is a perks bullet, not a work-location requirement, but it sat within 120 chars
# of four foreign cities and falsely tripped the HARD foreign-onsite rule. An
# "on-site"/"onsite" occurrence followed shortly by one of these words is dropped
# before the city-proximity check runs (both the foreign-location HARD rule and
# the PL anti-hybrid-city SOFT rule reuse this).
_ONSITE_PERKS_CONTEXT_RE = re.compile(
    r"\b(?:dining|lunch(?:es)?|snacks?|cafeteria|canteen|coffee|breakfast|"
    r"free\s+food|kitchen|parking|bike\s+storage|gym(?:\s+membership)?|"
    r"office\s+dog|perks?\s+and\s+rewards?)\b",
    re.IGNORECASE,
)


def _onsite_signal_positions(blob: str) -> list[int]:
    """Match starts of _ONSITE_SIGNAL_RES, minus perks-bullet noise (see above)."""
    return [
        m.start()
        for p in _ONSITE_SIGNAL_RES
        for m in p.finditer(blob)
        if not _ONSITE_PERKS_CONTEXT_RE.search(blob[m.start() : m.start() + 100])
    ]


# Strong fully-remote signals — if present, do NOT block on a body city mention.
#
# theprotocol.it's "Parametry oferty" block renders a per-listing "tryb pracy:"
# (work mode) value that is real, listing-specific data — NOT boilerplate — but
# it can enumerate several modes together (e.g. "zdalna • hybrydowa •
# stacjonarna" / "zdalna • hybrydowa"). Calibration (docs/DOOMED_GATE_PLAN.md
# M4) found several real SENT theprotocol.it jobs (NASK, B2BNet, IdeoSpZoO…)
# falsely hard-blocked because "stacjonarna"/"hybrydowa" sat next to the city —
# but "zdalna" was ALSO offered, meaning the candidate could simply pick the
# remote option. Whenever "zdalna" appears as (one of) the offered mode(s) in
# that facet, remote is available → veto the on-site block.
_FULLY_REMOTE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfully\s+remote\b",
        r"\b100\s*%\s*remote\b",
        r"\bremote[-\s]first\b",
        r"\bwork\s+from\s+anywhere\b",
        r"\bw\s+pełni\s+zdaln\w*",  # PL: fully remote
        r"\b100\s*%\s*zdaln\w*",  # PL: 100% remote
        r"tryb\s+pracy:?\s*\n?\s*\[?\s*(?:100\s*%\s*)?zdaln\w*",  # PL: theprotocol.it work-mode facet offering remote
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


def _is_acceptable_weekly_hybrid(job: Job) -> bool:
    """True → keep despite a Warsaw/Kraków hybrid, because it is only ~1 day/week.

    Owner accepts a once-a-week office commute to Warsaw or Kraków (reachable from
    Wrocław). Grants the exception only when (a) the text mentions Warsaw/Kraków,
    (b) NO other anti-hybrid city is mentioned (so a multi-office role abroad still
    fails), and (c) a low-frequency ("1 day a week" / "raz w tygodniu") signal is
    present. An unspecified or higher frequency does NOT qualify.
    """
    if not FILTER.get("allow_weekly_hybrid_warsaw_krakow", False):
        return False
    blob = f"{job.title or ''} {job.location or ''} {_job_plain_text_blob(job)}".lower()
    if not any(c in blob for c in _WEEKLY_HYBRID_CITIES):
        return False
    other_cities = _ANTI_HYBRID_CITIES - _WEEKLY_HYBRID_CITIES
    if any(c in blob for c in other_cities):
        return False
    return any(p.search(blob) for p in _WEEKLY_HYBRID_RES)


def _is_unwanted_onsite_location(job: Job) -> bool:
    """True → skip (body couples an on-site/hybrid signal with a far-away city).

    Complements _matches_location (which only sees title + location field): many
    listings show location="remote"/"Poland" but the description demands N days a
    week in a Kraków/Warsaw/Cyprus office. Requires the on-site signal and the city
    to sit within a short window of each other to avoid false positives on jobs
    that merely mention a head-office city in passing. A strong fully-remote signal,
    a Wrocław location, or an acceptable ~1-day/week Warsaw/Kraków hybrid vetoes it.
    """
    if not FILTER.get("exclude_body_onsite_city", False):
        return False
    loc = (job.location or "").lower()
    if "wroc" in loc:  # explicitly a Wrocław role — hybrid there is fine
        return False
    blob = _job_plain_text_blob(job).lower()
    if not blob.strip():
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
    # Office only ~1 day/week in Warsaw/Kraków → acceptable, do not block.
    return not _is_acceptable_weekly_hybrid(job)


def _is_ai_training_or_mill(job: Job) -> bool:
    """True → skip (known AI-data-labeling / staffing-mill company).

    Title-based "AI Training"/"data annotation" roles are already caught by
    exclude_patterns; this adds a company-name check for mills whose titles look
    like clean "Angular Developer" (micro1 fronts: QuikHireStaffing, HireFeed …).
    """
    if not FILTER.get("exclude_ai_training", False):
        return False
    company = (job.company or "").lower()
    if not company:
        return False
    return any(c in company for c in FILTER.get("exclude_companies", []))


# Owner decision 2026-07-12: skip Russia-tied roles outright, even remote ones
# — unclear whether a Russia-based employer can legally/practically pay a
# Poland-based candidate (banking/sanctions). Listing-level companion to the
# doomed gate's _assess_russia_market (hunter.filters.assess_job_text, fires
# on the fetched full body text): this one is cheap — title+location only,
# before any fetch — and catches sources whose location field itself
# literally names the country (worldwide-remote boards sometimes do).
_RUSSIA_MARKET_LOCATION_TOKENS = (
    "russia",
    "russian federation",
    "рф",
    "россия",
    "российская федерация",
)


def _is_russia_market(job: Job) -> bool:
    """True → skip (title/location ties the role to Russia, even if remote)."""
    blob = f"{job.location or ''} {job.title or ''}".lower()
    return any(tok in blob for tok in _RUSSIA_MARKET_LOCATION_TOKENS)


def _matches_location(job: Job) -> bool:
    """Check if job location matches allowed locations.

    Anti-hybrid-city logic (P-6.1): if the location or title contains a city
    in _ANTI_HYBRID_CITIES with no allowed location token (remote/wroclaw), the
    job is rejected even if the top-level location field says just 'Poland'.
    This catches LinkedIn listings where city appears only in the title.
    """
    locations = FILTER.get("locations", [])
    if not locations:
        return True

    loc = job.location.lower() if isinstance(job.location, str) else str(job.location).lower()

    # If any allowed token is present in location, accept immediately
    if any(token in loc for token in locations):
        return True

    # Check title for anti-hybrid cities — LinkedIn often puts city there
    # (e.g. "Angular Dev Kraków - Zabłocie" with location="Poland")
    title_lower = (job.title or "").lower()
    blob = f"{loc} {title_lower}"

    # If blob contains an anti-city but NO allowed token → reject
    has_anti_city = any(city in blob for city in _ANTI_HYBRID_CITIES)
    has_allowed = any(token in blob for token in locations)

    if has_anti_city and not has_allowed:
        return False

    # Allowed token found somewhere in location+title (e.g. "Wrocław" in title
    # with location="Poland") → accept
    if has_allowed:
        return True

    # Empty/blank location with no anti-city in the title → no geo information
    # at all; treat as unknown and let it through rather than silently dropping
    # a potentially remote offer.  Non-empty location that matched neither the
    # whitelist nor anti-cities (e.g. "Berlin", "Poland") → reject (strict
    # whitelist).
    return not loc.strip()


# Reason keys emitted by classify_job() / apply_filters_with_stats(). Kept here so
# callers (e.g. the Gmail hunt report) can rely on a stable, documented vocabulary.
FILTER_REASONS: tuple[str, ...] = (
    "title_kw",
    "require_angular",
    "level",
    "exclude_pattern",
    "react_no_angular",
    "location",
    "russia",
    "german",
    "contract",
    "relocation",
)


def classify_job(job: Job) -> str | None:
    """Return the reason a single job is filtered out, or None if it passes.

    The reason string is one of FILTER_REASONS. This is the per-job core that
    apply_filters_with_stats() aggregates; callers that need the reason for one
    specific job (the Gmail per-email report) reuse it directly so the report and
    the filter pipeline can never disagree.
    """
    # title_keywords and require_angular enforced for ALL sources, including
    # gmail_*. Recommendation-style alert digests (rekomendacje@wysylka.pracuj.pl,
    # NoFluffJobs "similar offers" blocks, LinkedIn "New jobs similar to ...")
    # pack 10–20 unrelated roles (.NET, PHP, database, DevOps, embedded …)
    # next to the headline FE one. Bypassing the title whitelist for gmail_*
    # used to let those through to AUTO_APPLY, burning LLM calls on irrelevant
    # roles. The cost of a false-negative (missing one ambiguous title like
    # "Software Engineer III") is much lower than the cost of a false-positive
    # (CV generated for a database/Go/PHP role we'd never apply to).
    if not _matches_title_keywords(job.title):
        return "title_kw"
    if not _requires_angular_check(job.title):
        return "require_angular"

    # Hard filters — apply to ALL sources including gmail_*
    if _is_excluded_level(job.title):
        return "level"
    if _is_react_only_title(job.title):
        return "react_no_angular"
    if _is_node_only_title(job.title):
        return "exclude_pattern"
    if _is_unwanted_fullstack(job):
        return "exclude_pattern"
    if _matches_exclude_pattern(job.title):
        return "exclude_pattern"
    if _is_ai_training_or_mill(job):
        return "exclude_pattern"
    if _is_react_without_angular(job):
        return "react_no_angular"
    if _has_body_disqualifier(job):
        return "exclude_pattern"
    if _is_russia_market(job):
        return "russia"
    # Location: a non-whitelisted location is rejected UNLESS it's an acceptable
    # ~1-day/week Warsaw/Kraków hybrid. The body on-site/city gate (which already
    # honours the same weekly exception) catches far cities hidden in the text.
    if not _matches_location(job) and not _is_acceptable_weekly_hybrid(job):
        return "location"
    if _is_unwanted_onsite_location(job):
        return "location"
    if _is_german_language_required(job):
        return "german"
    if _is_unacceptable_contract(job):
        return "contract"
    if _requires_relocation(job):
        return "relocation"
    return None


def apply_filters(jobs: list[Job]) -> list[Job]:
    """Filter jobs — returns passing jobs only. See apply_filters_with_stats for breakdown."""
    return apply_filters_with_stats(jobs)[0]


def apply_filters_with_stats(jobs: list[Job]) -> tuple[list[Job], dict[str, int]]:
    """Filter jobs and return (passing_jobs, reason_counts).

    All filters run uniformly for every source, including gmail_*. The gmail
    title-keyword bypass was removed in fix/gmail-enforce-title-keywords:
    recommendation-style digests pack unrelated roles (.NET, PHP, database,
    DevOps …) next to the headline FE one, and bypassing the whitelist let
    those through to AUTO_APPLY. Checks:
      - level exclusions  (intern / manager / tech lead)
      - title-only React check  (_is_react_only_title)
      - exclude_pattern  (Java, .NET, Magento, React Native …)
      - raw-skills React check  (_is_react_without_angular)
      - location check  (_matches_location — same whitelist as all sources)
      - German language requirement
      - unacceptable contract  (_is_unacceptable_contract — part-time / 1-month)
      - relocation required  (_requires_relocation — explicit relocation demand)

    reason_counts keys: title_kw, require_angular, level, exclude_pattern,
                        react_no_angular, location, russia, german, contract,
                        relocation
    """
    result = []
    reasons: dict[str, int] = dict.fromkeys(FILTER_REASONS, 0)

    for job in jobs:
        reason = classify_job(job)
        if reason is None:
            result.append(job)
        else:
            reasons[reason] += 1

    return result, reasons


# Human-readable labels for the manual-apply "warn but allow" screen. Maps each
# body-level gate to a short message shown in Telegram before docs are generated.
# screen_job_text() (paste path) treats ALL of these uniformly — its existing
# warn-but-allow contract is unaffected by the HARD/SOFT split below, which only
# matters for the NEW doomed gate (assess_job_text / apply_api / apply_cli).
#
# M4 calibration (docs/DOOMED_GATE_PLAN.md, docs/DOOMED_GATE_CALIBRATION.md)
# against ~375 real postings + owner Sent ground truth split these two ways:
# fullstack/German/contract/relocation/AI-mill never misfired on a row the
# owner actually sent → safe to SKIP generation outright (HARD). But
# has_body_disqualifier and is_unwanted_onsite_location (Polish-city version)
# DID hard-block real sent rows on genuine (non-artifact) content — a "Mile
# widziane: WordPress" nice-to-have among a dozen other optional tools
# (NASK_2), a "Magento or React/Vue" listing (AdvoxStudio), a flexible-hybrid
# Warsaw office the owner judged acceptable anyway (Bayer, PeopleVibe, Codest,
# TechRecruitmentAgency). Both checks stay high-value signals — just not
# precise enough to silently skip generation on — so they degrade to SOFT
# (warn, still generate) in the new gate instead.
_MANUAL_SCREEN_CHECKS_HARD: tuple[tuple[str, str], ...] = (
    ("_is_unwanted_fullstack", "fullstack role paired with a backend stack"),
    ("_is_ai_training_or_mill", "AI-training / staffing-mill company"),
    ("_is_german_language_required", "German language required"),
    ("_is_unacceptable_contract", "part-time / very short contract"),
    ("_requires_relocation", "relocation required"),
)
_MANUAL_SCREEN_CHECKS_SOFT: tuple[tuple[str, str], ...] = (
    ("_has_body_disqualifier", "excluded tech/platform in the description"),
    ("_is_unwanted_onsite_location", "on-site / hybrid outside Wrocław"),
)
# screen_job_text() (paste-path warn-but-allow) checks both tiers uniformly.
_MANUAL_SCREEN_CHECKS: tuple[tuple[str, str], ...] = (
    _MANUAL_SCREEN_CHECKS_HARD + _MANUAL_SCREEN_CHECKS_SOFT
)


# ── Doomed-vacancy gate (docs/DOOMED_GATE_PLAN.md) ────────────────────────────
# Deterministic, zero-LLM full-text screen that runs between fetch and the first
# LLM call. Two severities:
#   hard — high precision, skips generation entirely (see decision #2a-c in the
#          plan): non-Poland on-site/hybrid, non-EU work-authorization demands,
#          an unsupported required language, plus the reused _MANUAL_SCREEN_CHECKS.
#   soft — lower precision, judgment call: primary-stack mismatch (Vue/Svelte/
#          Ember-first posting with neither Angular nor React anywhere).


@dataclass(frozen=True)
class GateFinding:
    rule: str
    severity: str  # "hard" | "soft"
    evidence: str  # short human-readable quote/label for Telegram + logs


def _context_snippet(blob: str, start: int, end: int, pad: int = 40) -> str:
    """Short evidence quote around a regex match, trimmed of surrounding noise."""
    snippet = blob[max(0, start - pad) : min(len(blob), end + pad)]
    return re.sub(r"\s+", " ", snippet).strip()


# On-site/hybrid coupled with a location OUTSIDE Poland — no commute is possible
# for a Wrocław-based candidate, unlike the PL anti-hybrid cities above (which at
# least share a country and, for Warsaw/Kraków, an acceptable weekly exception).
# Deliberately conservative (word-boundary, no bare state abbreviations like "VA")
# to keep the false-positive rate near zero — see M4 calibration in the plan.
_FOREIGN_LOCATION_RE = re.compile(
    r"\b(?:"
    # US states (spelled out only — abbreviations are too ambiguous)
    r"virginia|california|texas|massachusetts|illinois|colorado|florida|"
    r"north\s+carolina|pennsylvania|ohio|michigan|arizona|new\s+jersey|"
    # US / Canada cities
    r"mclean|arlington|washington,?\s*d\.?c\.?|austin|san\s+francisco|"
    r"san\s+jose|seattle|chicago|boston|los\s+angeles|dallas|denver|atlanta|"
    r"miami|houston|phoenix|philadelphia|detroit|minneapolis|charlotte|"
    r"san\s+diego|portland|nashville|raleigh|new\s+york\s+city|"
    r"toronto|vancouver|montreal|ottawa|"
    # UK
    r"london|manchester|birmingham|edinburgh|glasgow|"
    # Western Europe (non-PL)
    r"berlin|munich|münchen|frankfurt|hamburg|cologne|köln|paris|amsterdam|"
    r"rotterdam|dublin|zurich|zürich|geneva|vienna|brussels|madrid|barcelona|"
    r"milan|milano|rome|stockholm|copenhagen|oslo|helsinki|lisbon|"
    # Country / region names
    r"united\s+states|u\.s\.a\.?|united\s+kingdom|canada|england|scotland|"
    r"germany|france|netherlands|switzerland|austria|belgium|spain|italy|"
    r"sweden|denmark|norway|finland|ireland"
    r")\b",
    re.IGNORECASE,
)


def _assess_foreign_onsite(job: Job, blob: str) -> "GateFinding | None":
    """HARD (a): on-site/hybrid signal sitting near a non-Poland location.

    Mirrors _is_unwanted_onsite_location's windowing (~120 chars) but against a
    location OUTSIDE Poland instead of the PL anti-hybrid city set. Vetoed by a
    strong fully-remote signal, any Wrocław mention (candidate's own city), or
    the acceptable ~1-day/week Warsaw/Kraków hybrid exception.
    """
    if any(p.search(blob) for p in _FULLY_REMOTE_RES):
        return None
    if "wroc" in blob:
        return None
    if _is_acceptable_weekly_hybrid(job):
        return None
    onsite_pos = _onsite_signal_positions(blob)
    if not onsite_pos:
        return None
    for m in _FOREIGN_LOCATION_RE.finditer(blob):
        if any(abs(m.start() - o) <= 120 for o in onsite_pos):
            return GateFinding(
                rule="foreign_onsite_hybrid",
                severity="hard",
                evidence=_context_snippet(blob, m.start(), m.end()),
            )
    return None


# Non-EU work-authorization / citizenship demands the candidate cannot satisfy
# (Polish/EU national, needs sponsorship-free EU work eligibility, not US/UK).
_WORK_AUTH_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bw-?2\s+(?:employee|employment|only|status)\b",
        r"\bc2c\b",
        r"\b1099\s+contractor\b",
        r"\bh-?1b\b",
        r"\bmust\s+be\s+a\s+(?:us|u\.s\.)\s+citizen\b",
        r"\b(?:us|u\.s\.)\s+citizenship\s+(?:is\s+)?required\b",
        r"\bgreen\s+card\s+(?:holder|required)\b",
        r"\bsecurity\s+clearance\s+required\b",
        r"\bactive\s+(?:secret|top\s+secret)\s+clearance\b",
        r"\bmust\s+(?:be\s+)?(?:located|based|reside|residing)\s+in\s+the\s+"
        r"(?:us|u\.s\.|united\s+states|uk|united\s+kingdom)\b",
        r"\bauthoriz(?:ed|ation)\s+to\s+work\s+in\s+the\s+(?:us|u\.s\.|united\s+states)"
        r"\s+without\s+sponsorship\b",
        r"\bno\s+visa\s+sponsorship\b",
        r"\bvisa\s+sponsorship\s+(?:is\s+)?not\s+(?:available|provided|offered)\b",
    )
)


def _assess_work_authorization(blob: str) -> "GateFinding | None":
    """HARD (b): posting demands work authorization the candidate cannot meet."""
    for p in _WORK_AUTH_RES:
        m = p.search(blob)
        if m:
            return GateFinding(
                rule="unsupported_work_authorization",
                severity="hard",
                evidence=_context_snippet(blob, m.start(), m.end()),
            )
    return None


# Required-language detection for languages the candidate does NOT speak, beyond
# German (already covered end-to-end by _is_german_language_required). Narrow,
# high-precision list — same "required/native/fluent/CEFR level" pattern shape.
_UNSUPPORTED_LANG_REQUIRED_RES: dict[str, tuple[re.Pattern[str], ...]] = {
    "French": tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bwith\s+french\b",
            r"\(french\)",
            r"\bfrench\s+speaking\b",
            r"\bspeaking\s+french\b",
            r"\bfluent\s+in\s+french\b",
            r"\bnative(?:[-\s]+level)?\s+french\b",
            r"\bfrench\s+native\b",
            r"\bfrench\s+(?:is\s+)?(?:required|mandatory|essential|a\s+must)\b",
            r"\b(?:c1|c2|b2|b1)[\s\-]*(?:\(\s*)?french\b",
            r"\bfrench\s*[\(:]?\s*(?:c1|c2|b2|b1)\b",
            r"\bcourant\s+en\s+français\b",
        )
    ),
    "Dutch": tuple(
        re.compile(p, re.IGNORECASE)
        for p in (
            r"\bwith\s+dutch\b",
            r"\(dutch\)",
            r"\bdutch\s+speaking\b",
            r"\bspeaking\s+dutch\b",
            r"\bfluent\s+in\s+dutch\b",
            r"\bnative(?:[-\s]+level)?\s+dutch\b",
            r"\bdutch\s+native\b",
            r"\bdutch\s+(?:is\s+)?(?:required|mandatory|essential|a\s+must)\b",
            r"\b(?:c1|c2|b2|b1)[\s\-]*(?:\(\s*)?dutch\b",
            r"\bdutch\s*[\(:]?\s*(?:c1|c2|b2|b1)\b",
        )
    ),
}

# English-as-working-language vetoes any required-foreign-language finding
# (shared with the German check's not-required set).
_ENGLISH_ONLY_WORKPLACE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"\benglish\s+is\s+(?:the\s+)?(?:working|company|office)\s+language\b", re.IGNORECASE
    ),
    re.compile(r"\bworking\s+language\s*[:\s]+\s*english\b", re.IGNORECASE),
)


def _assess_unsupported_language(job: Job) -> "GateFinding | None":
    """HARD (c): a required language (French/Dutch) the candidate doesn't speak.

    German is intentionally excluded — already covered end-to-end by
    _is_german_language_required (listing-level filter + manual-screen reuse
    above); this only extends the same pattern shape to a narrow extra list.
    """
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return None
    if any(p.search(blob) for p in _ENGLISH_ONLY_WORKPLACE_RES):
        return None
    for lang_name, patterns in _UNSUPPORTED_LANG_REQUIRED_RES.items():
        if any(p.search(blob) for p in patterns):
            return GateFinding(
                rule="unsupported_language_required",
                severity="hard",
                evidence=f"{lang_name} language required",
            )
    return None


# SOFT — primary-stack mismatch: a Vue/Svelte/Ember-first posting where neither
# Angular nor React appears anywhere in the text. "Angular or Vue" / "React or
# Vue" postings are NOT flagged (both frameworks present → not a mismatch).
_OTHER_FRAMEWORK_RE = re.compile(
    r"\b(?:vue(?:\.?js)?|sveltekit|svelte|ember(?:\.?js)?)\b", re.IGNORECASE
)
# SOFT — game-engine-first role (Pixi/Cocos/Phaser/Babylon/Haxe/Unity/…). These
# postings often carry "Frontend Developer" + "TypeScript" (so the title/level
# filters pass) but want a game-rendering stack the candidate doesn't have —
# real case 2026-07-12: a Nexters "Hero Wars" role reached generation at 82%
# because the old Vue/Svelte-only rule couldn't see the mismatch. Tokens are
# specific enough to avoid English-word false positives (no bare "spine"/
# "unity"); still SOFT (warn only), so an occasional miss just adds a warning.
_GAME_ENGINE_RE = re.compile(
    r"\b(?:"
    r"pixi(?:\.?js)?|"
    r"cocos(?:2d)?(?:\s*creator)?|cocoscreator|"
    r"phaser|"
    r"babylon(?:\.?js)?|"
    r"haxe|"
    r"spine\s+sdk|"
    r"godot|"
    r"gamemaker|"
    r"unreal(?:\s+engine)?|"
    r"unity\s*(?:3d|engine)"
    r")\b",
    re.IGNORECASE,
)
_CANDIDATE_FRAMEWORK_RE = re.compile(r"\b(?:angular|react(?:\.?js)?)\b", re.IGNORECASE)


def _assess_stack_mismatch(blob: str) -> "GateFinding | None":
    """SOFT — primary stack isn't Angular/React.

    Two shapes, same guard (only when neither Angular nor React appears): a
    Vue/Svelte/Ember-first web role, or a game-engine-first role.
    """
    if _CANDIDATE_FRAMEWORK_RE.search(blob):
        return None
    other = _OTHER_FRAMEWORK_RE.search(blob)
    if other:
        return GateFinding(
            rule="stack_mismatch_non_candidate_framework",
            severity="soft",
            evidence=_context_snippet(blob, other.start(), other.end()),
        )
    engine = _GAME_ENGINE_RE.search(blob)
    if engine:
        return GateFinding(
            rule="stack_mismatch_game_engine",
            severity="soft",
            evidence=_context_snippet(blob, engine.start(), engine.end()),
        )
    return None


def _assess_mill_body(blob: str) -> "GateFinding | None":
    """HARD — a known AI-training / staffing-mill name in the job BODY.

    `_is_ai_training_or_mill` only sees `job.company`, which is blank for
    Gmail-alert stubs (linkedin.com enrichment is skipped via
    GMAIL_ENRICH_SKIP_HOSTS) — exactly how the micro1 fronts
    (QuikHireStaffing, HireFeed) slipped through to generation on
    2026-07-06. The mill's own name or its apply link (micro1.com) usually
    appears in the posting text itself, so scan the full blob for every
    `exclude_companies` entry. "micro1" also matches "micro1.com" (the
    trailing lookahead only rejects word characters).
    """
    if not FILTER.get("exclude_ai_training", False):
        return None
    for name in FILTER.get("exclude_companies", []):
        pattern = re.compile(
            r"(?<!\w)" + re.escape(name.lower()).replace(r"\ ", r"\s+") + r"(?!\w)"
        )
        m = pattern.search(blob)
        if m:
            return GateFinding(
                rule="ai_mill_body",
                severity="hard",
                evidence=_context_snippet(blob, m.start(), m.end()),
            )
    return None


# Russia-tied roles, even remote ones — owner decision 2026-07-12 after two
# talanto.work "Remote · Russia" postings reached generation via the Telegram
# channels source (rabotafrontend): it's unclear whether a Russia-based
# employer can legally/practically pay a Poland-based candidate (banking/
# sanctions), so these are skipped outright regardless of remote status.
# Deliberately requires the location TAG to sit right next to "Remote"/
# "Location"/"Локация" rather than matching a bare "Russia" mention — real
# talanto.work pages render a sitewide sidebar ("By Region: Jobs in Europe /
# USA / Canada / Russia") that would false-positive on every single posting
# on the site if "russia" alone were enough.
_RUSSIA_MARKET_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bremote\s*[·•\-–—:]\s*russia\b",
        r"\bremote\s*[·•\-–—:]\s*russian\s+federation\b",
        r"\blocation\s*:?\s*russia\b",
        r"\blocation\s*:?\s*russian\s+federation\b",
        r"\bлокация\s*:?\s*рф\b",
        r"\bлокация\s*:?\s*росси(?:я|йская\s+федерация)\b",
        # "по ТК РФ" (per the Russian Labor Code) — a common outstaff/agency
        # phrasing (real example: talanto.work Extyl posting, tag says only
        # "Middle · Remote" with no country, but the description reads
        # "Оформление в штат компании ... по ТК РФ") — near-zero false-positive
        # risk, this abbreviation has no other meaning in a job posting.
        r"\bтк\s+рф\b",
    )
)


def _assess_russia_market(blob: str) -> "GateFinding | None":
    """HARD — the posting's own location tag ties the role to Russia."""
    for p in _RUSSIA_MARKET_RES:
        m = p.search(blob)
        if m:
            return GateFinding(
                rule="russia_remote_market",
                severity="hard",
                evidence=_context_snippet(blob, m.start(), m.end()),
            )
    return None


# Full-page dumps append unrelated recommendation/navigation blocks that don't
# describe THIS job at all — real examples from calibration (docs/DOOMED_GATE_
# PLAN.md M4):
#   - LinkedIn "Similar jobs"/"People also viewed": a Fairmarkit dump contained
#     an unrelated "... (hybrid work in Warsaw) — Synergetica" sidebar entry
#     that falsely tripped the on-site/hybrid check for Fairmarkit itself.
#   - theprotocol.it renders a sitewide SEO footer starting at "Praca w
#     miastach:" (jobs by city) listing dozens of cities/positions/technologies
#     as "<term> praca" links (e.g. "Wordpress praca", "Praca IT Kraków") that
#     have nothing to do with the listing — falsely tripped both the body-
#     disqualifier and on-site-city checks on real SENT jobs (NASK, ProcomSystem,
#     B2BNet, Devapo, EdgeOneSolutions, ConsdataSA, IdeoSpZoO, GetItTogether…).
#   - pracuj.pl appends a "Sprawdź podobne oferty" (check similar offers) block.
#   - talanto.work renders a sitewide faceted-search sidebar starting "By
#     Region: Jobs in Europe / USA / Canada / Russia / By Format: Remote
#     Jobs / ... / Hybrid Jobs / Office Jobs" on EVERY job page — "Hybrid"/
#     "Office" sitting near "USA"/"Canada" within the on-site-signal window
#     falsely tripped _assess_foreign_onsite on a genuinely fully-remote
#     posting (real example: talanto.work/jobs/3d657ccb-...).
# Cut the text at the first such marker before any body-level check runs.
_RECOMMENDATION_TAIL_RE = re.compile(
    # No trailing \b: "praca w miastach:" ends in ':' (non-word), so a \b right
    # after it never matches (': ' and ':\n' are both non-word→non-word — the
    # same class of bug as the historical `\bc#\b` miss on "C#", see CLAUDE.md).
    r"\n\s*(?:similar jobs\b|people also viewed\b|show more jobs like this\b"
    r"|praca w miastach:|sprawdź podobne oferty\b|by region\b)",
    re.IGNORECASE,
)


def _strip_recommendation_tail(text: str) -> str:
    m = _RECOMMENDATION_TAIL_RE.search(text or "")
    return text[: m.start()] if m else text


# Boilerplate lines that precede the real title on common job-board dumps
# (LinkedIn's raw HTML fetch starts with site chrome before the title).
_TITLE_GUESS_JUNK_RE = re.compile(
    r"^(?:skip to main content|sign in|home|menu|search|apply|save|"
    r"see who .* has hired|\d+\s+(?:days?|weeks?|hours?|minutes?)\s+ago)$",
    re.IGNORECASE,
)

# A guessed line must LOOK like a job title before the title-based gate rules
# may act on it: it has to name a role (EN+PL role nouns) or an explicit
# frontend stack keyword. A pasted Telegram/chat dump often opens with
# conversational prose ("Да, тут можно ознакомиться с компанией — plavno.io",
# owner report 2026-07-11) and the old "first meaningful line" rule turned
# that into the gate's "title", producing garbage off_domain_title warnings.
# The real calibration wins keep matching (".NET Developer (Angular)" →
# developer; "Software Engineer - QuantumBlack, AI by McKinsey" → engineer),
# and a miss is safe by design: no guess just means the title-based checks
# find nothing, exactly like before the paste-path extension.
_TITLE_GUESS_ROLE_RE = re.compile(
    r"\b(?:developer|engineer|programmer|architect|consultant|specialist|"
    r"designer|analyst|tester(?:ka)?|devops|lead|manager|"
    r"front[- ]?end|full[- ]?stack|angular|react|javascript|typescript|"
    r"programist(?:a|ka)|in[żz]ynier|deweloper|specjalist(?:a|ka)|"
    r"projektant(?:ka)?|kierownik)\b",
    re.IGNORECASE,
)

# A job title sits near the top of a posting — stop guessing after this many
# plausible candidate lines so a role noun buried deep in the body can't be
# mistaken for the title.
_TITLE_GUESS_MAX_CANDIDATES = 10


def _guess_title_from_text(job_text: str) -> str:
    """Best-effort job-title guess from the first title-looking line of raw text.

    Used ONLY on the manual-paste path, where no title is known at gate time
    (the LLM hasn't parsed the posting yet) — see docs/DOOMED_GATE_PASTE_PLAN.md.
    A line only qualifies if it names a role or stack keyword
    (_TITLE_GUESS_ROLE_RE) and doesn't end like a prose sentence; the scan is
    capped to the first few candidate lines. A miss just means the title-based
    checks below find nothing (same as before the paste-path extension); it
    never overrides an explicitly known title, so a wrong guess cannot turn
    into a false positive on a job whose real title is known.
    """
    candidates = 0
    for line in (job_text or "").splitlines():
        line = line.strip()
        if not line or len(line) < 4 or len(line) > 120:
            continue
        if _TITLE_GUESS_JUNK_RE.match(line):
            continue
        candidates += 1
        if candidates > _TITLE_GUESS_MAX_CANDIDATES:
            break
        # Titles don't end like sentences; chat intros and body prose do
        # ("Senior Angular/TypeScript experience; deep understanding…" must
        # not become the "title" just because it names the stack).
        if line.endswith((".", "!", "?", "…", ":", ";", ",")):
            continue
        if _TITLE_GUESS_ROLE_RE.search(line):
            return line
    return ""


def _assess_title_exclude_pattern(effective_title: str) -> "GateFinding | None":
    """HARD — the (explicit or guessed) title names an excluded backend/CMS
    stack (.NET/Java/C#/PHP/Vue/Magento/…), same list as the listing-level
    _matches_exclude_pattern. Real calibration case: Santander ".NET Developer
    (Angular)" — no "fullstack" in the title (so _is_unwanted_fullstack never
    applies), but ".NET" alone is exactly what the listing-level filter would
    have caught had this not been a manual paste."""
    if not effective_title:
        return None
    if _matches_exclude_pattern(effective_title):
        return GateFinding(
            rule="title_exclude_pattern",
            severity="hard",
            evidence=effective_title[:80],
        )
    return None


def _assess_off_domain_title(effective_title: str, *, was_guessed: bool) -> "GateFinding | None":
    """SOFT — the (explicit or guessed) title doesn't match the frontend
    title-keyword whitelist. SOFT rather than HARD: a GUESSED title is
    inherently less reliable than a known one (wrong-line risk), so an
    incorrect hard block is too costly here — warn and let the owner decide.
    Real calibration case: QuantumBlackMcKinsey "Software Engineer -
    QuantumBlack, AI by McKinsey" — a full stack/AI role, not a frontend one."""
    if not effective_title:
        return None
    if not _matches_title_keywords(effective_title):
        return GateFinding(
            rule="off_domain_title",
            severity="soft",
            evidence=effective_title[:80] + (" (guessed)" if was_guessed else ""),
        )
    return None


# NOTE: an earlier iteration of this plan added a SOFT rule that flagged any
# anti-hybrid city named near the top of the text (no onsite/hybrid wording
# required) to catch header-only cases like Comarch ("Comarch Warsaw,
# Mazowieckie, Poland" with no "hybrid"/"onsite" anywhere in the body).
# Recalibration against the real corpus immediately proved it too noisy even
# at SOFT: Fairmarkit — a real, fully described, SENT (98% ATS) Warsaw-office
# EU role with no hybrid language of its own — tripped it exactly the same as
# Comarch. A bare city mention in a header can't be told apart from "this is
# just where the company's office happens to be"; the rule was removed
# rather than shipped as noise on good jobs (docs/DOOMED_GATE_PASTE_PLAN.md).


def assess_job_text(job_text: str, *, title: str = "", company: str = "") -> list[GateFinding]:
    """Deterministic, zero-LLM doomed-vacancy gate over the full fetched job text.

    Returns every finding (hard + soft, in check order); callers decide what to
    do with them (see hunter.apply_api / hunter.apply_cli wiring). Reuses the
    existing _MANUAL_SCREEN_CHECKS body-level filters (split HARD/SOFT per M4
    calibration, see the comment above _MANUAL_SCREEN_CHECKS_HARD), plus HARD
    rule families for non-Poland on-site/hybrid, unsupported work
    authorization, unsupported required language, and an excluded backend/CMS
    stack named in the title; SOFT rules for primary-stack mismatch and an
    off-domain (non-frontend) title. The title-based rules
    (docs/DOOMED_GATE_PASTE_PLAN.md) use the explicit `title` when known,
    otherwise a best-effort guess from the raw text — see
    `_guess_title_from_text`. No network calls, no LLM calls — pure regex.
    """
    job_text = _strip_recommendation_tail(job_text or "")
    job = Job(
        title=title or "",
        company=company or "",
        location="",
        salary=None,
        url="",
        source="manual",
        raw={"description": job_text or ""},
    )
    blob = f"{job.title}\n{_job_plain_text_blob(job)}".lower()

    findings: list[GateFinding] = []
    for checks, severity in (
        (_MANUAL_SCREEN_CHECKS_HARD, "hard"),
        (_MANUAL_SCREEN_CHECKS_SOFT, "soft"),
    ):
        for fn_name, label in checks:
            fn = globals().get(fn_name)
            try:
                if fn and fn(job):
                    findings.append(
                        GateFinding(rule=fn_name.strip("_"), severity=severity, evidence=label)
                    )
            except Exception:  # noqa: BLE001 — one bad check must not sink the others
                continue

    for assess in (_assess_foreign_onsite,):
        try:
            finding = assess(job, blob)
            if finding:
                findings.append(finding)
        except Exception:  # noqa: BLE001
            pass

    try:
        finding = _assess_unsupported_language(job)
        if finding:
            findings.append(finding)
    except Exception:  # noqa: BLE001
        pass

    for assess_blob in (
        _assess_work_authorization,
        _assess_mill_body,
        _assess_russia_market,
        _assess_stack_mismatch,
    ):
        try:
            finding = assess_blob(blob)
            if finding:
                findings.append(finding)
        except Exception:  # noqa: BLE001
            pass

    # Title-based checks (docs/DOOMED_GATE_PASTE_PLAN.md) — reuse the explicit
    # title when known (hunt/JobLeads), otherwise fall back to a best-effort
    # guess from the raw text. Only meaningful on the manual-paste path, where
    # no title is known at gate time; a guess miss just finds nothing.
    was_guessed = not bool(title)
    effective_title = title or _guess_title_from_text(job_text)
    try:
        finding = _assess_title_exclude_pattern(effective_title)
        if finding:
            findings.append(finding)
    except Exception:  # noqa: BLE001
        pass
    try:
        finding = _assess_off_domain_title(effective_title, was_guessed=was_guessed)
        if finding:
            findings.append(finding)
    except Exception:  # noqa: BLE001
        pass

    return findings


def screen_job_text(
    job_text: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
) -> str | None:
    """Body-level screen for the manual URL/paste 'warn but allow' path.

    A manually pasted URL bypasses the hunt-time filter entirely. This runs
    assess_job_text() (plus the supplied title/company when known) and returns
    the first finding's evidence — regardless of severity — as a short
    human-readable reason, so the bot can warn the user before generating docs
    while still letting it through (always warn-only here — this function
    never blocks; the actual blocking decision is `run_doomed_gate`'s, at
    Step 1.5f). Returns None when nothing fires.

    Since docs/DOOMED_GATE_PASTE_PLAN.md, this DOES surface an off-domain-title
    warning (the SOFT `off_domain_title` rule inside assess_job_text) when the
    known/guessed title fails the frontend title-keyword whitelist — a plain
    paste is no longer treated as "the owner already knows what they're
    pasting" for that signal, since real calibration data (QuantumBlackMcKinsey,
    a fullstack/AI role generated for $0.18 nobody wanted) showed that
    assumption cost real money. `location` is accepted for backward
    compatibility but unused (no call site has ever passed a meaningful value;
    assess_job_text derives geography from the body text itself).
    """
    findings = assess_job_text(job_text, title=title, company=company)
    return findings[0].evidence if findings else None
