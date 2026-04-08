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


def _is_react_without_angular(job: Job) -> bool:
    """Skip React-only jobs: check title AND raw skills/tech data from API."""
    if not FILTER.get("exclude_react_without_angular", False):
        return False

    title = job.title.lower()
    raw = job.raw or {}

    # Collect all tech-related text from raw API data
    tech_texts = [title]

    # JustJoin: raw["skills"] = [{"name": "React.js", "level": "senior"}, ...]
    for skill in raw.get("skills", []):
        tech_texts.append((skill.get("name") or "").lower())

    # NoFluffJobs: raw["technology"] = "react" and raw["tiles"] with values
    tech_texts.append((raw.get("technology") or "").lower())
    for tile in raw.get("tiles", {}).get("values", []):
        tech_texts.append((tile.get("value") or "").lower())

    # NoFluffJobs: raw["category"] can be string or dict
    cat = raw.get("category", "")
    if isinstance(cat, str):
        tech_texts.append(cat.lower())

    combined = " ".join(tech_texts)
    has_react = bool(re.search(r"\breact\b", combined))
    has_angular = "angular" in combined
    return has_react and not has_angular


def _matches_location(job: Job) -> bool:
    """Check if job location matches allowed locations.

    LinkedIn results skip this check — they are already geo-filtered by the API
    (geoId parameter restricts to Poland).
    """
    if job.source == "linkedin":
        return True

    locations = FILTER.get("locations", [])
    if not locations:
        return True
    loc = job.location.lower()
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
