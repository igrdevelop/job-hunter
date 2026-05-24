import html
import re

from hunter.models import Job
from hunter.config import FILTER

_HTML_TAG_RE = re.compile(r"<[^>]+>", re.DOTALL)

# If any pattern matches job text → treat as German required (skip job),
# unless a german_not_required pattern matches first.
_GERMAN_REQUIRED_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
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
    # Front-end signals — don't block if any of these are present
    _FE_SIGNALS = (
        "angular", "frontend", "front-end", "react", "vue",
        " ui ", "ui/ux", "spa", " ux",
    )
    if any(sig in t for sig in _FE_SIGNALS):
        return False

    # Node.js in title + absence of FE signals = backend/full-stack role
    node_patterns = (
        r"\bnode\.?js\b",
        r"\bnode\s+developer\b",
        r"\bnode\s+engineer\b",
    )
    return any(re.search(p, t, re.IGNORECASE) for p in node_patterns)


def _is_react_only_title(title: str) -> bool:
    """Return True when the job title signals React-only with no Angular involvement.

    Title-only check that runs for ALL sources (including gmail_*) before the
    more expensive raw-data check.  Catches "React Developer", "React Native
    Engineer", "Frontend (React)" etc. that slip through the Gmail bypass.

    Only triggers when 'angular' is absent from the title.
    """
    if not FILTER.get("exclude_react_without_angular", False):
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


def _is_german_language_required(job: Job) -> bool:
    """True → skip job (German appears to be a hard requirement)."""
    if not FILTER.get("exclude_german_language_required", False):
        return False
    blob = _job_plain_text_blob(job)
    if not blob.strip():
        return False
    if any(p.search(blob) for p in _GERMAN_NOT_REQUIRED_RES):
        return False
    return any(p.search(blob) for p in _GERMAN_REQUIRED_RES)


# Cities where hybrid work is NOT acceptable (too far from Wrocław).
# A job whose location or title contains one of these AND doesn't contain an
# allowed location token (remote/wroclaw) is rejected.
# LinkedIn often returns "Poland" as location with the city in the title (e.g.
# "Jlabs Angular Dev Kraków - Zabłocie"), so we check BOTH location and title.
_ANTI_HYBRID_CITIES: frozenset[str] = frozenset({
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
})


def _matches_location(job: Job) -> bool:
    """Check if job location matches allowed locations.

    Anti-hybrid-city logic (П-6.1): if the location or title contains a city
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

    # No positive match and no anti-city exclusion → fall back to whitelist
    return False  # location didn't match any allowed token


def apply_filters(jobs: list[Job]) -> list[Job]:
    """Filter jobs — returns passing jobs only. See apply_filters_with_stats for breakdown."""
    return apply_filters_with_stats(jobs)[0]


def apply_filters_with_stats(jobs: list[Job]) -> tuple[list[Job], dict[str, int]]:
    """Filter jobs and return (passing_jobs, reason_counts).

    Gmail-sourced jobs (source starts with 'gmail_') bypass only the
    title-keyword and location checks — the user's alert subscriptions
    already pre-filter for relevance/location.

    However, hard safety filters run for ALL sources including gmail_*:
      - level exclusions  (intern / manager / tech lead)
      - title-only React check  (_is_react_only_title)
      - exclude_pattern  (Java, .NET, Magento, React Native …)
      - raw-skills React check  (_is_react_without_angular)
      - German language requirement

    reason_counts keys: title_kw, require_angular, level, exclude_pattern,
                        react_no_angular, location, german
    """
    result = []
    reasons: dict[str, int] = {
        "title_kw": 0,
        "require_angular": 0,
        "level": 0,
        "exclude_pattern": 0,
        "react_no_angular": 0,
        "location": 0,
        "german": 0,
    }

    for job in jobs:
        is_gmail = job.source.startswith("gmail_")

        # ── Title-keyword / require-angular — Gmail bypass (pre-filtered by alerts) ──
        if not is_gmail:
            if not _matches_title_keywords(job.title):
                reasons["title_kw"] += 1
                continue
            if not _requires_angular_check(job.title):
                reasons["require_angular"] += 1
                continue

        # ── Hard filters — apply to ALL sources including gmail_* ──────────────────

        # Level/seniority exclusions (intern, manager, tech lead, etc.)
        if _is_excluded_level(job.title):
            reasons["level"] += 1
            continue

        # Title-only React check (П-3.1) — catches "React Developer" from any source
        if _is_react_only_title(job.title):
            reasons["react_no_angular"] += 1
            continue

        # Node.js backend title check (П-5.1) — catches "TypeScript/Node.js Developer"
        # where 'backend' is absent but the role is clearly BE
        if _is_node_only_title(job.title):
            reasons["exclude_pattern"] += 1
            continue

        # Exclude-pattern: Java, .NET, Magento, React Native, Node backend …
        if _matches_exclude_pattern(job.title):
            reasons["exclude_pattern"] += 1
            continue

        # Raw-skills React check (needs API data — may be absent for gmail_*)
        if _is_react_without_angular(job):
            reasons["react_no_angular"] += 1
            continue

        # ── Location — Gmail bypass (alert geo already restricts) ──────────────────
        if not is_gmail:
            if not _matches_location(job):
                reasons["location"] += 1
                continue

        # German language requirement — check full text blob for all sources
        if _is_german_language_required(job):
            reasons["german"] += 1
            continue

        result.append(job)

    return result, reasons
