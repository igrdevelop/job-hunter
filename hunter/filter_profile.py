"""Per-user filter profile loader (docs/FILTERS_YAML_PLAN.md M1).

Layer 1 — ``builtin_defaults()``: today's FILTER values (moved from
``filter_config.py``), one shared copy for everyone, code-reviewed.

Layer 2 — optional user YAML (``users/{uid}/candidate/filters.yaml`` later;
any path for tests / M4). Merged on top per the knob table's replace /
extend_only strategies. Missing file ⇒ Layer 1 as-is (owner behavior today,
byte-for-byte).

Cache is keyed by ``(filters path, filters mtime, candidate path, candidate
mtime)`` — NOT a plain ``@lru_cache`` on path alone — so an external writer
(API ``PUT /filters``, M5) or a ``candidate.yaml`` home-city edit is picked
up without a bot restart. A missing file caches on ``(path, None)``.

``hunter.filter_config.FILTER = load_profile()`` keeps every existing
``from hunter.config import FILTER`` import working unchanged.
"""

from __future__ import annotations

import copy
import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

from hunter import candidate

logger = logging.getLogger(__name__)

_DEFAULT_HOME_ALIASES = ["wrocław", "wroclaw"]

# Knob merge strategies (docs/FILTERS_YAML_PLAN.md §Knob-by-knob map).
# Keys not listed here are ignored with a warning when present in a user file.
_REPLACE_KEYS = frozenset(
    {
        "title_keywords",
        "require_title_terms",
        "require_angular",  # legacy alias → require_title_terms
        "exclude_levels",
        "exclude_patterns",
        "exclude_stacks_without",
        "exclude_react_without_angular",  # legacy alias → exclude_stacks_without
        "exclude_fullstack_with_backend",
        "fullstack_backend_stacks",
        "exclude_body_disqualifiers",
        "body_exclude_patterns",
        "exclude_body_onsite_city",
        "allow_low_frequency_hybrid",
        "exclude_ai_training",
        "exclude_german_language_required",
        "exclude_unacceptable_contract",
        "exclude_relocation_required",
    }
)
_EXTEND_KEYS = frozenset(
    {
        "exclude_companies",
        "extra_anti_hybrid_cities",
    }
)
# Derived from candidate.yaml — never taken from filters.yaml.
_DERIVED_KEYS = frozenset({"locations"})

# Entries that must compile as regex; invalid ones are dropped with a warning.
_PATTERN_KEYS = frozenset(
    {
        "exclude_patterns",
        "body_exclude_patterns",
        "fullstack_backend_stacks",
    }
)

_BOOL_KEYS = frozenset(
    {
        "require_angular",
        "exclude_react_without_angular",
        "exclude_fullstack_with_backend",
        "exclude_body_disqualifiers",
        "exclude_body_onsite_city",
        "allow_low_frequency_hybrid",
        "exclude_ai_training",
        "exclude_german_language_required",
        "exclude_unacceptable_contract",
        "exclude_relocation_required",
    }
)
_LIST_KEYS = frozenset(
    {
        "title_keywords",
        "require_title_terms",
        "exclude_levels",
        "exclude_patterns",
        "fullstack_backend_stacks",
        "body_exclude_patterns",
        "exclude_companies",
        "extra_anti_hybrid_cities",
    }
)
# exclude_stacks_without is dict | None — checked specially in _type_ok.

# (filters_path, filters_mtime, candidate_path, candidate_mtime) → profile
_cache: dict[tuple[str | None, int | None, str | None, int | None], dict[str, Any]] = {}


def clear_profile_cache() -> None:
    """Drop the load_profile cache (tests / forced reload)."""
    _cache.clear()


def _mtime_ns(path: Path | None) -> int | None:
    if path is None or not path.exists():
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def builtin_defaults() -> dict[str, Any]:
    """Layer 1 — today's FILTER verbatim (WHY-comments preserved from filter_config).

    ``locations`` is derived from candidate.yaml each call (same formula the
    old module-level FILTER used).
    """
    # ── Job filters ───────────────────────────────────────────────────────────
    # Angular-only: title must match at least one keyword AND contain "angular"
    # (unless it's a generic "frontend"/"typescript" title — require_angular
    # catches those)
    return {
        # Title must contain at least ONE of these (case-insensitive)
        "title_keywords": [
            "angular",
            "frontend",
            "front-end",
            "javascript",
            "typescript",
        ],
        # Terms that MUST appear in every title (AND). Empty = off. Generalizes
        # the old require_angular bool (docs/FILTERS_YAML_PLAN.md M2).
        "require_title_terms": [],
        # Legacy alias — kept in sync by _sync_legacy_aliases for source modules
        # that still read FILTER["require_angular"].
        "require_angular": False,
        "exclude_levels": [
            "junior",
            "intern",
            "internship",
            "trainee",
            "stażysta",
            "praktykant",
            "staz",
            # P-8.1: management / leadership / non-IC roles
            "tech lead",
            "tech-lead",
            "techlead",
            # RU spellings of "tech lead" (owner request 2026-08-06: block in
            # every spelling, EN + RU — substring match, lowercased, so this
            # also catches "технический лидер").
            "техлид",
            "тех-лид",
            "тех лид",
            "технический лид",
            # Team-lead roles (owner request 2026-08-08, from Sent-notes audit:
            # "тимлид" rejections were never filtered — only "tech lead" was).
            # Substring match also catches "Team Leader" / "тимлидер".
            "team lead",
            "teamlead",
            "team-lead",
            "тимлид",
            "тим-лид",
            "тим лид",
            # RU spellings of intern/trainee (RU-market Telegram channels relay
            # titles like "Стажер Frontend developer" — the EN/PL intern entries
            # above can't see them). "стажиров" catches "стажировка"/"стажировки".
            "стажер",
            "стажёр",
            "стажиров",
            "project lead",
            "engineering manager",
            "head of engineering",
            "vp of engineering",
            "cto",
            # Part-time — not relevant for full-time search
            "part-time",
            "part time",
            "parttime",
        ],
        # Always accept: fully remote regardless of city, plus the candidate's
        # home city (on-site OR hybrid — hybrid elsewhere is rejected). Aliases
        # come from candidate.yaml (location.home_city_aliases); default
        # preserves the project owner's original Wrocław-based list when
        # candidate.yaml is absent.
        "locations": ["remote", "zdalnie", "zdalna"]
        + list(candidate.get("location.home_city_aliases", list(_DEFAULT_HOME_ALIASES))),
        # Title matching ANY regex → skip
        "exclude_patterns": [
            r"\bjava\b",
            r"\.net",
            # NOTE: trailing \b after "#" never matches ("#" is a non-word char,
            # so there is no word boundary between "#" and the following space).
            # Use a leading boundary only so "C#", "(C#", "C#/Angular" are all
            # caught.
            r"\bc#",
            r"\bphp\b",
            r"\bqa\b",
            r"\bsdet\b",
            r"quality\s+assurance",
            r"test\s+automation",
            # fullstack WITHOUT angular is handled by
            # _is_fullstack_without_angular() in filters.py — we don't put it
            # in exclude_patterns so Angular fullstack passes.
            r"\bbackend\b",
            r"\bback-end\b",
            r"\bvue\b",
            r"\bnuxt\b",
            r"\bmagento\b",
            r"\bruby\b",
            # P-3.3: React Native — mobile-only, not FE web
            r"\breact\s+native\b",
            r"\breact[- ]native\b",
            # P-4.1: eCommerce/CMS platforms — not web-FE stack
            r"\bhyv[äa]\b",  # Hyva (Magento theme) — Finnish spelling variants
            r"\badobe\s+commerce\b",  # Adobe Commerce = Magento rebranded
            r"\bpwa\s+studio\b",  # Magento PWA Studio
            r"\bshopware\b",
            r"\bshopify\b",
            r"\bbigcommerce\b",
            r"\bwoocommerce\b",
            r"\bdrupal\b",
            r"\bwordpress\b",
            r"\bsharepoint\b",
            r"\bsap\b",
            # P-7.1: Salesforce / DevOps / SRE / mobile / test-automation roles
            r"\bsalesforce\b",
            r"\bdevops\b",
            r"\bdev-ops\b",
            r"\bsre\b",  # Site Reliability Engineer
            r"\bplatform\s+engineer\b",
            r"\bcloud\s+engineer\b",
            r"\binfrastructure\s+engineer\b",
            r"\bandroid\b",
            r"\bios\s+developer\b",
            r"\bswift\s+developer\b",
            r"\bkotlin\s+developer\b",
            r"\bflutter\b",
            r"\bautomation\s+engineer\b",
            r"\btesting\s+engineer\b",
            # P-8.1: management / non-IC roles (regex for mixed-case not caught
            # by exclude_levels)
            r"\btech\s+lead\b",
            r"\bproject\s+lead\b",
            r"\bpart[- ]?time\b",
            # Low-code / non-web-FE platforms and niche roles the candidate skips
            r"\bmendix\b",
            r"\boutsystems\b",
            r"\blow[-\s]?code\b",
            r"\bemail\s+developer\b",
            r"\bui\s+designer\b",
            # AI data-labeling / "AI training" gig roles (not real FE engineering)
            r"\bai\s+train(?:ing|er)\b",
            r"\bai\s+tutor\b",
            r"\bdata\s+annotat\w*\b",
            r"\bdata\s+label(?:l)?ing\b",
        ],
        # Skip jobs that mention a blocked stack without the "unless" term
        # (default: React without Angular). Generalizes exclude_react_without_angular
        # (docs/FILTERS_YAML_PLAN.md M2). null disables the rule.
        "exclude_stacks_without": {"unless": "angular", "block": ["react"]},
        # Legacy alias — kept in sync by _sync_legacy_aliases for source modules.
        "exclude_react_without_angular": True,
        # Fullstack policy: a "Full Stack / Fullstack" title with NO Angular is
        # always blocked (handled in filters._is_unwanted_fullstack). When
        # Angular IS present the role is blocked only if it is paired with a
        # *heavy backend* stack below (checked in title AND body). Node/Nuxt
        # are deliberately NOT in this list, so a JS/Node fullstack-with-Angular
        # role still passes (per owner's preference).
        "exclude_fullstack_with_backend": True,
        "fullstack_backend_stacks": [
            r"\bjava\b",
            r"\bspring(?:\s+boot)?\b",
            r"\.net\b",
            r"\basp\.net\b",
            r"\bc#",
            r"\bpython\b",
            r"\bdjango\b",
            r"\bgolang\b",
            r"\bphp\b",
            r"\bruby\s+on\s+rails\b",
        ],
        # Disqualifiers hidden in the job BODY (title looks like clean FE, but
        # the description reveals a stack/platform the candidate doesn't want).
        # Checked against the full job text blob, mirroring the
        # German/contract/relocation gates.
        "exclude_body_disqualifiers": True,
        "body_exclude_patterns": [
            r"\bblazor\b",
            r"\bmendix\b",
            r"\boutsystems\b",
            r"\blow[-\s]?code\b",
            r"\bwordpress\b",
            r"\bdrupal\b",
            r"\bmagento\b",
            r"\bsharepoint\b",
        ],
        # Reject when the BODY couples an on-site / hybrid signal with a city
        # outside the Wrocław area (the listing's location field frequently
        # says "remote"/"Poland" while the description demands N days/week in a
        # Kraków/Warsaw/foreign office).
        "exclude_body_onsite_city": True,
        # Exception to the two location gates above: KEEP a hybrid role in a
        # Polish city outside Wrocław when the office visits are LOW-FREQUENCY
        # — about once a week or less (a couple of times a month, monthly,
        # quarterly, occasional visits). Owner decision 2026-08-08 (Sent-notes
        # audit): the header often says just "hybrid" while the description
        # clarifies the visits are rare — the frequency phrasing in the body
        # wins. More than 1 day/week, an unspecified frequency, or a non-Polish
        # city → still rejected.
        # (Broadened from the old Warsaw/Kraków-only 1-day/week exception,
        # config key renamed from allow_weekly_hybrid_warsaw_krakow.)
        "allow_low_frequency_hybrid": True,
        # Reject AI-data-labeling / staffing-mill roles by company name (titles
        # are often clean "Angular Developer" so only the company gives them
        # away — micro1 fronts).
        "exclude_ai_training": True,
        "exclude_companies": [
            "micro1",
            "alignerr",
            "quikhire",
            "hirefeed",
            "mercor",
            "outlier ai",
        ],
        # Drop roles that require German (checked in title + location + raw
        # description-like fields). Set false if you speak German or use boards
        # where this produces false positives.
        "exclude_german_language_required": True,
        # Drop part-time / very short contract roles (checked in full job text,
        # not only title). Catches cases where "part-time" appears in the
        # description but not the job title.
        "exclude_unacceptable_contract": True,
        # Drop jobs that explicitly require relocation outside Poland / outside
        # Wrocław region. Catches "hybrid Helsinki", "relocation to Barcelona
        # required", etc. in the full text.
        "exclude_relocation_required": True,
        # Extra anti-hybrid cities appended to _ANTI_HYBRID_CITIES in filters.py.
        # These are non-Polish cities that appeared as hybrid requirements in
        # the tracker.
        "extra_anti_hybrid_cities": [
            # EU cities outside Poland that appeared in hybrid job descriptions
            "helsinki",
            "helsingfors",
            "barcelona",
            "madrid",
            "lisbon",
            "lisboa",
            "berlin",
            "munich",
            "münchen",
            "hamburg",
            "frankfurt",
            "amsterdam",
            "rotterdam",
            "prague",
            "brno",
            "bratislava",
            "budapest",
            "bucharest",
            "sofia",
            "zagreb",
            # Cyprus (recruiter posts / XM, GRS — hybrid in Limassol/Nicosia/Larnaca)
            "limassol",
            "nicosia",
            "larnaca",
            "larnaka",
            "paphos",
            "pafos",
            # Non-EU / remote-but-actually-not regions
            "islamabad",
            "karachi",
            "lahore",  # Pakistan
            "bangalore",
            "mumbai",
            "delhi",  # India
            "singapore",
            "dubai",
            "abu dhabi",
            "hong kong",
            "tokyo",
        ],
    }


def _resolve_path(path: str | Path | None) -> Path | None:
    if path is not None:
        return Path(path)
    env = os.environ.get("FILTERS_YAML_PATH")
    if env:
        return Path(env)
    # Single-user default: filters.yaml next to candidate.yaml (M3).
    # M4 will pass an explicit per-user path via users.user_paths().
    cand_env = os.environ.get("CANDIDATE_YAML_PATH")
    if cand_env:
        return Path(cand_env).expanduser().resolve().parent / "filters.yaml"
    return Path(__file__).resolve().parent.parent / "candidate" / "filters.yaml"


def _home_city_aliases() -> set[str]:
    aliases = candidate.get("location.home_city_aliases", list(_DEFAULT_HOME_ALIASES))
    out = {str(a).lower() for a in (aliases or [])}
    home = candidate.get("location.home_city", "Wrocław")
    if home:
        out.add(str(home).lower())
    return out


def _carve_home_city(profile: dict[str, Any]) -> None:
    """Subtract the profile's own home-city aliases from extra_anti_hybrid_cities.

    A user whose home city is IN the anti-hybrid list (e.g. Berlin) must not
    have their own city rejected — see docs/FILTERS_YAML_PLAN.md Problem.
    """
    aliases = _home_city_aliases()
    cities = profile.get("extra_anti_hybrid_cities") or []
    profile["extra_anti_hybrid_cities"] = [c for c in cities if str(c).lower() not in aliases]


def _validate_patterns(profile: dict[str, Any], *, source: str) -> None:
    for key in _PATTERN_KEYS:
        pats = profile.get(key)
        if not isinstance(pats, list):
            continue
        kept: list[str] = []
        for i, pat in enumerate(pats):
            if not isinstance(pat, str):
                logger.warning(
                    "filters profile %s: %s[%d] is not a string — dropped",
                    source,
                    key,
                    i,
                )
                continue
            try:
                re.compile(pat)
            except re.error as exc:
                logger.warning(
                    "filters profile %s: %s[%d] invalid regex %r (%s) — dropped",
                    source,
                    key,
                    i,
                    pat,
                    exc,
                )
                continue
            kept.append(pat)
        profile[key] = kept


def _type_ok(key: str, value: Any) -> bool:
    if key == "exclude_stacks_without":
        return value is None or (isinstance(value, dict) and "block" in value)
    if key in _BOOL_KEYS:
        return isinstance(value, bool)
    if key in _LIST_KEYS:
        return isinstance(value, list)
    return True


def _apply_legacy_user_aliases(user: dict[str, Any]) -> dict[str, Any]:
    """Translate legacy keys in a user file into the canonical M2 knobs.

    - ``require_angular: true`` → ``require_title_terms: ["angular"]``
      (only when the user did not also set ``require_title_terms``).
    - ``exclude_react_without_angular: true/false`` → ``exclude_stacks_without``
      dict / null (only when ``exclude_stacks_without`` absent).
    Canonical keys always win over legacy when both are present.
    """
    out = dict(user)
    if "require_title_terms" not in out and "require_angular" in out:
        out["require_title_terms"] = ["angular"] if out.get("require_angular") else []
    if "exclude_stacks_without" not in out and "exclude_react_without_angular" in out:
        out["exclude_stacks_without"] = (
            {"unless": "angular", "block": ["react"]}
            if out.get("exclude_react_without_angular")
            else None
        )
    return out


def _sync_legacy_aliases(profile: dict[str, Any]) -> None:
    """Keep legacy bools in sync so source modules reading old keys still work.

    ``require_angular`` means "slug/title must contain angular" for scrapers
    (JustJoin/Bulldogjob). It must be True only when ``angular`` is among
    ``require_title_terms`` — a React-seeker profile with terms ``[react]``
    must NOT flip scrapers into an angular-only slug gate.
    """
    terms = profile.get("require_title_terms")
    if not isinstance(terms, list):
        terms = []
        profile["require_title_terms"] = terms
    profile["require_angular"] = any(str(t).lower() == "angular" for t in terms)

    rule = profile.get("exclude_stacks_without")
    if isinstance(rule, dict):
        unless = str(rule.get("unless") or "").lower()
        block = [str(b).lower() for b in (rule.get("block") or [])]
        profile["exclude_react_without_angular"] = unless == "angular" and "react" in block
    else:
        profile["exclude_stacks_without"] = None
        profile["exclude_react_without_angular"] = False


def _extend_list(base: list, extra: list) -> list:
    """Append user entries not already present (case-insensitive for strings)."""
    seen: set[str] = set()
    out: list = []
    for item in list(base) + list(extra):
        marker = str(item).lower() if isinstance(item, str) else repr(item)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(item)
    return out


def _merge_user(profile: dict[str, Any], user: dict[str, Any], *, source: str) -> None:
    for key, value in user.items():
        if key in _DERIVED_KEYS:
            logger.warning(
                "filters profile %s: key %r is derived from candidate.yaml — ignored",
                source,
                key,
            )
            continue
        if key in _REPLACE_KEYS:
            if not _type_ok(key, value):
                logger.warning(
                    "filters profile %s: key %r has wrong type %s — kept default",
                    source,
                    key,
                    type(value).__name__,
                )
                continue
            profile[key] = copy.deepcopy(value)
            continue
        if key in _EXTEND_KEYS:
            if not isinstance(value, list):
                logger.warning(
                    "filters profile %s: key %r must be a list — kept default",
                    source,
                    key,
                )
                continue
            profile[key] = _extend_list(list(profile.get(key) or []), value)
            continue
        logger.warning(
            "filters profile %s: unknown key %r — ignored",
            source,
            key,
        )


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001 — never raise from the loader
        logger.warning("filters profile %s: failed to read (%s) — using builtins", path, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "filters profile %s: root must be a mapping, got %s — using builtins",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _build_profile(path: Path | None) -> dict[str, Any]:
    profile = builtin_defaults()
    if path is not None and path.exists():
        user = _apply_legacy_user_aliases(_load_yaml(path))
        if user:
            _merge_user(profile, user, source=str(path))
        _validate_patterns(profile, source=str(path))
    else:
        # Builtin patterns are code-reviewed; still validate so a typo in
        # builtin_defaults surfaces as a warning rather than a hunt crash.
        _validate_patterns(profile, source="<builtin>")
    _sync_legacy_aliases(profile)
    _carve_home_city(profile)
    # locations always re-derived after merge (user file cannot set them)
    profile["locations"] = ["remote", "zdalnie", "zdalna"] + list(
        candidate.get("location.home_city_aliases", list(_DEFAULT_HOME_ALIASES))
    )
    return profile


def load_profile(path: str | Path | None = None) -> dict[str, Any]:
    """Deep-copy Layer 1, merge user YAML (if any), cache by path+mtime keys.

    Cache key includes ``candidate.yaml`` mtime so home-city / location aliases
    refresh when that file changes (even if filters.yaml is untouched). On a
    cache miss we clear ``candidate``'s path-only LRU so ``candidate.get``
    sees the current file.

    ``path=None`` resolves ``FILTERS_YAML_PATH`` / default ``candidate/filters.yaml``.
    """
    resolved = _resolve_path(path)
    filters_key = str(resolved.resolve()) if resolved is not None else None
    filters_mtime = _mtime_ns(resolved)
    cand_path = candidate._resolve_path()
    cand_key = str(cand_path.resolve())
    cand_mtime = _mtime_ns(cand_path)
    key = (filters_key, filters_mtime, cand_key, cand_mtime)
    cached = _cache.get(key)
    if cached is not None:
        return copy.deepcopy(cached)
    # Path-only candidate LRU would otherwise keep stale home_city_aliases.
    candidate._load_file.cache_clear()
    profile = _build_profile(resolved)
    _cache[key] = profile
    return copy.deepcopy(profile)
