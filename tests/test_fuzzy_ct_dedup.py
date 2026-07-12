"""
Tests for P-1.2: TrackerCache.is_fuzzy_ct() — fuzzy title similarity dedup.

Catches Gmail-enriched title variants that pass exact dedup_key but
describe the same job at the same company.
"""

import asyncio

from hunter.tracker_cache import TrackerCache


def _make_cache(*rows: tuple[str, str, str]) -> TrackerCache:
    """Build a TrackerCache populated with (id, company, title) tuples."""
    cache = TrackerCache()
    cache._loaded = True
    for row_id, company, title in rows:
        row = dict.fromkeys(
            [
                "Date",
                "Company",
                "Job Title",
                "Stack",
                "ATS %",
                "URL",
                "Folder",
                "Sent",
                "Re-application",
                "To Learn",
                "ID",
                "Drive URL",
                "Confirmation",
                "Answer",
            ],
            "",
        )
        row["ID"] = row_id
        row["Company"] = company
        row["Job Title"] = title
        cache.rows[row_id] = row
        cache.by_ctkey[f"{company.lower()}|{title.lower()}"] = row_id
    return cache


# ---------------------------------------------------------------------------
# Positive cases — fuzzy match should return True
# ---------------------------------------------------------------------------


def test_fuzzy_ct_remote_prefix() -> None:
    """'Remote Angular Developer' should match stored 'Angular Developer'."""
    cache = _make_cache(("id1", "Acme", "Angular Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "Remote Angular Developer"))
    assert result is True


def test_fuzzy_ct_senior_prefix() -> None:
    """'Senior Angular Developer' should match stored 'Angular Developer' (senior in stop-list)."""
    cache = _make_cache(("id1", "Acme", "Angular Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "Senior Angular Developer"))
    assert result is True


def test_fuzzy_ct_marketing_tail_stripped_before_compare() -> None:
    """'Remote Angular Developer — Build great UIs' should match 'Angular Developer'."""
    cache = _make_cache(("id1", "Acme", "Angular Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "Remote Angular Developer — Build great UIs"))
    assert result is True


def test_fuzzy_ct_company_variation() -> None:
    """Company legal-suffix variation should still match."""
    cache = _make_cache(("id1", "Acme Sp. z o.o.", "Frontend Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("ACME", "Frontend Developer"))
    assert result is True


# ---------------------------------------------------------------------------
# Negative cases — fuzzy match should return False
# ---------------------------------------------------------------------------


def test_fuzzy_ct_different_technology() -> None:
    """'React Developer' should NOT match 'Angular Developer'."""
    cache = _make_cache(("id1", "Acme", "Angular Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "React Developer"))
    assert result is False


def test_fuzzy_ct_different_role() -> None:
    """'Frontend Developer' should NOT match 'Backend Developer'."""
    cache = _make_cache(("id1", "Acme", "Frontend Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "Backend Developer"))
    assert result is False


def test_fuzzy_ct_different_company() -> None:
    """Same title but different company → no match."""
    cache = _make_cache(("id1", "Acme", "Angular Developer"))
    result = asyncio.run(cache.is_fuzzy_ct("OtherCorp", "Angular Developer"))
    assert result is False


def test_fuzzy_ct_empty_cache() -> None:
    """Empty cache always returns False."""
    cache = TrackerCache()
    cache._loaded = True
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "Angular Developer"))
    assert result is False


def test_fuzzy_ct_below_threshold() -> None:
    """Single shared token out of 2 → score 0.5 < 0.6 → no match."""
    # "TypeScript" vs "TypeScript Developer": tokens {"typescript"} vs {"typescript","developer"}
    # score = 1/2 = 0.5
    cache = _make_cache(("id1", "Acme", "TypeScript"))
    result = asyncio.run(cache.is_fuzzy_ct("Acme", "TypeScript Developer"))
    assert result is False
