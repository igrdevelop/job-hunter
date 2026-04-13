import re

from hunter.models import Job
from hunter.config import FILTER


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

    # NoFluffJobs: raw["technology"] = str; SolidJobs: list[{"name": "IT"}, ...]
    _append_technology_field(tech_texts, raw.get("technology"))
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


def _matches_location(job: Job) -> bool:
    """Check if job location matches allowed locations (all sources including LinkedIn)."""
    locations = FILTER.get("locations", [])
    if not locations:
        return True
    loc = job.location.lower() if isinstance(job.location, str) else str(job.location).lower()
    return any(token in loc for token in locations)


def apply_filters(jobs: list[Job]) -> list[Job]:
    """Filter a list of Jobs according to config.FILTER rules."""
    result = []
    for job in jobs:
        if not _matches_title_keywords(job.title):
            continue
        if not _requires_angular_check(job.title):
            continue
        if _is_excluded_level(job.title):
            continue
        if _matches_exclude_pattern(job.title):
            continue
        if _is_react_without_angular(job):
            continue
        if not _matches_location(job):
            continue
        result.append(job)
    return result
