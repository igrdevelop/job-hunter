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
        r"\bangular\b", r"\bfrontend\b", r"\bfront-end\b",
        r"\breact\b", r"\bvue\b",
        r"\bui\b",      # "UI / Node.js Developer" — UI is FE
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
# Extra cities from FILTER["extra_anti_hybrid_cities"] (config.py) are merged in
# at module load time so the set is computed once and stays O(1) per lookup.
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
        r"\bstacjonarn\w*",          # PL: praca stacjonarna
        r"\bin[-\s]the[-\s]office\b",
        r"\bin[-\s]office\b",
        r"\bdays?\s+(?:a|per)\s+week\b",   # "3 days a week" (in office)
        r"\bdays?\s+in\s+the\s+office\b",
        r"\bz\s+biura\b",            # PL: from the office
        r"\bw\s+biurze\b",          # PL: in the office
    )
)

# Strong fully-remote signals — if present, do NOT block on a body city mention.
_FULLY_REMOTE_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bfully\s+remote\b",
        r"\b100\s*%\s*remote\b",
        r"\bremote[-\s]first\b",
        r"\bwork\s+from\s+anywhere\b",
        r"\bw\s+pełni\s+zdaln\w*",   # PL: fully remote
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
    onsite_pos = [m.start() for p in _ONSITE_SIGNAL_RES for m in p.finditer(blob)]
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

    # Location field is empty/blank and title has no anti-city → we have no
    # geo information at all.  Treat as unknown: let it through rather than
    # silently dropping a potentially remote offer.
    if not loc.strip():
        return True

    # Non-empty location that matched neither the whitelist nor anti-cities
    # (e.g. "Berlin", "Poland") → reject (strict whitelist).
    return False


# Reason keys emitted by classify_job() / apply_filters_with_stats(). Kept here so
# callers (e.g. the Gmail hunt report) can rely on a stable, documented vocabulary.
FILTER_REASONS: tuple[str, ...] = (
    "title_kw",
    "require_angular",
    "level",
    "exclude_pattern",
    "react_no_angular",
    "location",
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
                        react_no_angular, location, german, contract, relocation
    """
    result = []
    reasons: dict[str, int] = {key: 0 for key in FILTER_REASONS}

    for job in jobs:
        reason = classify_job(job)
        if reason is None:
            result.append(job)
        else:
            reasons[reason] += 1

    return result, reasons


# Human-readable labels for the manual-apply "warn but allow" screen. Maps each
# body-level gate to a short message shown in Telegram before docs are generated.
_MANUAL_SCREEN_CHECKS: tuple[tuple[str, str], ...] = (
    ("_is_unwanted_fullstack", "fullstack role paired with a backend stack"),
    ("_has_body_disqualifier", "excluded tech/platform in the description"),
    ("_is_ai_training_or_mill", "AI-training / staffing-mill company"),
    ("_is_unwanted_onsite_location", "on-site / hybrid outside Wrocław"),
    ("_is_german_language_required", "German language required"),
    ("_is_unacceptable_contract", "part-time / very short contract"),
    ("_requires_relocation", "relocation required"),
)


def screen_job_text(
    job_text: str,
    *,
    title: str = "",
    company: str = "",
    location: str = "",
) -> str | None:
    """Body-level screen for the manual URL/paste 'warn but allow' path.

    A manually pasted URL bypasses the hunt-time filter entirely. This runs the
    gates that work on the fetched full text (plus the supplied title/company when
    known) and returns a short human-readable reason if the posting *would* have
    been filtered — so the bot can warn the user before generating docs, while
    still letting it through. Returns None when nothing fires.

    Deliberately does NOT enforce the title-keyword whitelist: a manual paste is
    an intentional override, so we only flag disqualifiers we're confident about.
    """
    job = Job(
        title=title or "",
        company=company or "",
        location=location or "",
        salary=None,
        url="",
        source="manual",
        raw={"description": job_text or ""},
    )
    for fn_name, label in _MANUAL_SCREEN_CHECKS:
        fn = globals().get(fn_name)
        try:
            if fn and fn(job):
                return label
        except Exception:  # noqa: BLE001 — best-effort warning, never block apply
            continue
    return None
