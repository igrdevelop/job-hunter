"""Input validation helpers for the apply pipeline."""
import re

MIN_JOB_TEXT_LEN = 300  # P-2.2: raised from 200 — real postings are rarely <300 chars

_BOGUS_NAMES: frozenset[str] = frozenset({
    "unknown",
    "unknowncompany",
    "pracujportal",
    "generaljobboard",
    "generaljobposting",
    "generaljobsearch",
    "jobportal",
    "portal",
})


def is_bogus_company(name: str) -> bool:
    """Return True if the LLM-extracted company name is a placeholder or portal name."""
    normalized = re.sub(r"[^a-z0-9]", "", (name or "").lower().strip())
    return not normalized or normalized in _BOGUS_NAMES


def is_job_text_too_short(text: str, min_len: int = MIN_JOB_TEXT_LEN) -> bool:
    """Return True if the fetched job text is too short to be a real posting."""
    return len((text or "").strip()) < min_len
