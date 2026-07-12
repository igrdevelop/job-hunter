"""Tests for B1 (bogus company names) and B2 (short job text) validation."""

import pytest

from hunter.validation import is_bogus_company, is_job_text_too_short, MIN_JOB_TEXT_LEN


# ---------------------------------------------------------------------------
# B1 — bogus company name detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "Unknown",
        "UNKNOWN",
        "UnknownCompany",
        "PracujPortal",
        "pracujportal",
        "GeneralJobBoard",
        "GeneralJobPosting",
        "GeneralJobSearch",
        "generaljobboard",
    ],
)
def test_is_bogus_company_rejects_placeholders(name: str) -> None:
    assert is_bogus_company(name), f"Expected bogus but got False for: {name!r}"


@pytest.mark.parametrize(
    "name",
    [
        "EdgeOneSolutions",
        "Capgemini",
        "LinkGroup",
        "Upvanta",
        "NASK",
        "DCVTechnologies",
        "Arcanys",
        "GetItTogether",
    ],
)
def test_is_bogus_company_accepts_real_names(name: str) -> None:
    assert not is_bogus_company(name), f"Expected real company but got True for: {name!r}"


def test_is_bogus_company_empty_string_is_bogus() -> None:
    assert is_bogus_company("")


def test_is_bogus_company_none_coerced_is_bogus() -> None:
    # Caller may pass content.get("company_name", "") which returns ""
    assert is_bogus_company("")


# ---------------------------------------------------------------------------
# B2 — minimum job text length
# ---------------------------------------------------------------------------


def test_is_job_text_too_short_empty() -> None:
    assert is_job_text_too_short("")


def test_is_job_text_too_short_whitespace_only() -> None:
    assert is_job_text_too_short("   \n\t  ")


def test_is_job_text_too_short_below_threshold() -> None:
    assert is_job_text_too_short("Short text." * 5)  # ~55 chars


def test_is_job_text_too_short_at_threshold_is_ok() -> None:
    text = "x" * MIN_JOB_TEXT_LEN
    assert not is_job_text_too_short(text)


def test_is_job_text_too_short_long_text_is_ok() -> None:
    text = "We are looking for a Senior Angular Developer. " * 20  # ~940 chars
    assert not is_job_text_too_short(text)


def test_is_job_text_too_short_custom_threshold() -> None:
    text = "x" * 100
    assert is_job_text_too_short(text, min_len=200)
    assert not is_job_text_too_short(text, min_len=50)


def test_is_job_text_too_short_llm_placeholder() -> None:
    # "No specific job posting found" — real case from tracker row~292
    assert is_job_text_too_short("No specific job posting found.")
