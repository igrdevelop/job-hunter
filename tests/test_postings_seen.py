"""Tests for hunter/postings_seen.py — the market-memory listing ledger.

Isolated on a temp DB by monkeypatching ``hunter.postings_seen.DB_PATH``
(same technique as tests/test_source_health.py). No network, no LLM.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from hunter import filter_profile, postings_seen
from hunter.filters import _PL_ANTI_HYBRID_CITIES
from hunter.location_parse import HYBRID, REMOTE, _fold
from hunter.models import Job
from hunter.postings_seen import (
    RecordStats,
    _skills_from_raw,
    build_row,
    count_rows,
    get_row,
    prune,
    record_listings,
)
from hunter.tracker import normalize_url

T0 = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(hours=5)
NOW_ISO = T0.isoformat(timespec="seconds")


@pytest.fixture()
def seen_db(tmp_path, monkeypatch):
    db = tmp_path / "seen.db"
    monkeypatch.setattr(postings_seen, "DB_PATH", db)
    return db


def _pl_city() -> str:
    """A Polish anti-hybrid city that is NOT the candidate's home city."""
    home = {_fold(a) for a in filter_profile._home_city_aliases()}
    for c in sorted(_PL_ANTI_HYBRID_CITIES):
        if _fold(c) not in home:
            return c
    raise AssertionError("no non-home PL city in the vocabulary")


def _job(
    url: str = "https://justjoin.it/job-offer/acme-senior-angular?utm_source=x",
    *,
    title: str = "Senior Angular Developer",
    company: str = "Acme Sp. z o.o.",
    location: str = "Remote",
    salary: str | None = "20 000 - 26 000 PLN B2B",
    source: str = "justjoin",
    raw: dict | None = None,
) -> Job:
    return Job(
        title=title,
        company=company,
        location=location,
        salary=salary,
        url=url,
        source=source,
        raw=raw or {},
    )


JUSTJOIN_RAW = {
    "title": "Senior Angular Developer",
    "companyName": "Acme Sp. z o.o.",
    "city": "Remote",
    "workplaceType": "remote",
    "skills": None,  # the flat key is always null since 2026-08-12
    "requiredSkills": [
        {"name": "Angular", "level": 5},
        {"name": "TypeScript", "level": 4},
        {"name": "RxJS", "level": 4},
    ],
    "niceToHaveSkills": [
        {"name": "NgRx", "level": 3},
        {"name": "angular", "level": 1},  # case-insensitive dup of Angular
        {"name": "", "level": 2},  # empty name, dropped
    ],
}

NOFLUFF_RAW = {
    "title": "Frontend Developer (Angular)",
    "name": "Beta Software",
    "url": "frontend-developer-angular-beta",
    "fullyRemote": False,
    "location": {"places": [{"city": "Some City"}]},
    "salary": {"from": 15000, "to": 20000, "currency": "PLN", "type": "b2b"},
    "requirements": {
        "musts": [{"value": "Angular", "type": "main"}, {"value": "Sass"}],
        "nices": [{"value": "Jest"}],
    },
}


# ── build_row ─────────────────────────────────────────────────────────────────


def test_build_row_justjoin_shape_fills_every_derived_column():
    job = _job(raw=JUSTJOIN_RAW)
    row = build_row(job, "passed", now=NOW_ISO)

    assert row["url_norm"] == normalize_url(job.url)
    assert "utm_source" not in row["url_norm"]
    assert row["url"] == job.url
    assert row["source"] == "justjoin"
    assert row["first_seen"] == row["last_seen"] == NOW_ISO
    assert row["seen_count"] == 1
    assert row["company_norm"] == "acme"  # legal suffix stripped by normalize_company
    assert row["remote_mode"] == REMOTE
    assert row["city"] == ""
    # Salary: the Polish-board monthly figure with B2B.
    assert row["salary_raw"] == "20 000 - 26 000 PLN B2B"
    assert row["salary_min"] == 20000.0
    assert row["salary_max"] == 26000.0
    assert row["salary_currency"] == "PLN"
    assert row["salary_period"] == "month"
    assert row["salary_contract"] == "b2b"
    assert row["salary_monthly_min"] == 20000.0
    assert row["salary_monthly_max"] == 26000.0
    assert row["lang"] in ("EN", "PL")
    assert json.loads(row["skills_listing"]) == ["Angular", "TypeScript", "RxJS", "NgRx"]
    assert row["filter_verdict"] == row["filter_verdict_last"] == "passed"
    assert len(row["text_hash"]) == 40


def test_build_row_nofluff_shape_reads_requirements_values():
    city = _pl_city()
    job = _job(
        url="https://nofluffjobs.com/pl/job/frontend-developer-angular-beta",
        title="Frontend Developer (Angular)",
        company="Beta Software",
        location=f"{city.title()} (Hybrid)",
        salary="15 000 - 20 000 PLN B2B",
        source="nofluffjobs",
        raw=NOFLUFF_RAW,
    )
    row = build_row(job, "location", now=NOW_ISO)
    assert json.loads(row["skills_listing"]) == ["Angular", "Sass", "Jest"]
    assert row["remote_mode"] == HYBRID
    assert row["city"] == _fold(city)
    assert row["company_norm"] == "beta"  # "software" is a generic token
    assert row["filter_verdict"] == "location"


def test_build_row_no_salary_gives_null_numerics_and_empty_strings():
    row = build_row(_job(salary=None, raw={}), "passed", now=NOW_ISO)
    for col in ("salary_min", "salary_max", "salary_monthly_min", "salary_monthly_max"):
        assert row[col] is None
    for col in ("salary_raw", "salary_currency", "salary_period", "salary_contract"):
        assert row[col] == ""
    assert row["skills_listing"] == ""


def test_text_hash_is_stable_and_title_sensitive():
    a = build_row(_job(), "passed", now=NOW_ISO)["text_hash"]
    b = build_row(_job(), "title_kw", now="2027-01-01T00:00:00+00:00")["text_hash"]
    c = build_row(_job(title="Senior React Developer"), "passed", now=NOW_ISO)["text_hash"]
    assert a == b  # verdict / timestamp are not part of the hash
    assert a != c


def test_text_hash_never_touches_raw_posting_text():
    """The hash covers listing attributes only — a body in raw must not move it."""
    a = build_row(_job(raw={}), "passed", now=NOW_ISO)["text_hash"]
    b = build_row(_job(raw={"description": "a" * 5000}), "passed", now=NOW_ISO)["text_hash"]
    assert a == b


@pytest.mark.parametrize(
    "raw, expected",
    [
        ({}, []),
        (None, []),
        ({"skills": ["Angular", " TypeScript ", "angular", ""]}, ["Angular", "TypeScript"]),
        (
            {"attributes": [{"attributeName": "Angular", "groupName": "Technologies"}, {"x": 1}]},
            ["Angular"],
        ),
        ({"technology": "Angular"}, ["Angular"]),
        ({"requiredSkills": "not-a-list-but-a-string"}, ["not-a-list-but-a-string"]),
        ({"requiredSkills": 42}, []),
        ({"requirements": "flat string, not a dict"}, []),
    ],
)
def test_skills_from_raw_shapes(raw, expected):
    assert _skills_from_raw(raw) == expected


def test_skills_from_raw_capped_and_deduped():
    raw = {"skills": [f"Skill{i}" for i in range(100)] + ["skill1"]}
    out = _skills_from_raw(raw)
    assert len(out) == postings_seen.MAX_SKILLS
    assert len({s.lower() for s in out}) == len(out)


# ── record_listings ──────────────────────────────────────────────────────────


def test_table_created_lazily_on_first_call(seen_db):
    assert not seen_db.exists()
    assert count_rows() == 0
    with sqlite3.connect(seen_db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        idx = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "postings_seen" in names
    assert {"idx_postings_seen_last", "idx_postings_seen_company"} <= idx


def test_insert_then_rerecord_bumps_only_last_seen_side(seen_db):
    jobs = [
        _job(raw=JUSTJOIN_RAW),
        _job(url="https://nofluffjobs.com/pl/job/x", source="nofluffjobs", raw=NOFLUFF_RAW),
        _job(url="https://example.com/jobs/3", source="remotive", salary=None),
    ]
    first = record_listings([(j, "passed") for j in jobs], now=T0)
    assert first == RecordStats(inserted=3, updated=0, skipped_no_url=0)
    assert count_rows() == 3

    # Same sweep again, later, with a changed verdict (a filters.yaml edit).
    second = record_listings([(j, "title_kw") for j in jobs], now=T1)
    assert second == RecordStats(inserted=0, updated=3, skipped_no_url=0)
    assert count_rows() == 3

    row = get_row(normalize_url(jobs[0].url))
    assert row is not None
    assert row["seen_count"] == 2
    assert row["first_seen"] == T0.isoformat(timespec="seconds")
    assert row["last_seen"] == T1.isoformat(timespec="seconds")
    assert row["filter_verdict"] == "passed"  # as of first_seen — never overwritten
    assert row["filter_verdict_last"] == "title_kw"


def test_rerecord_never_overwrites_first_seen_attributes(seen_db):
    job = _job(salary="20 000 - 26 000 PLN B2B", raw=JUSTJOIN_RAW)
    record_listings([(job, "passed")], now=T0)
    changed = _job(
        title="Totally Different Title",
        company="Other Co",
        salary="1 - 2 EUR",
        location="Berlin (On-site)",
        raw={},
    )
    record_listings([(changed, "passed")], now=T1)
    row = get_row(normalize_url(job.url))
    assert row is not None
    assert row["title"] == "Senior Angular Developer"
    assert row["company"] == "Acme Sp. z o.o."
    assert row["salary_min"] == 20000.0
    assert row["location_raw"] == "Remote"
    assert json.loads(row["skills_listing"])[0] == "Angular"
    assert row["seen_count"] == 2


def test_duplicate_url_norm_within_one_batch_counts_once(seen_db):
    a = _job(url="https://example.com/jobs/1?utm_source=a", title="First wins")
    b = _job(url="https://example.com/jobs/1/", title="Second loses")
    stats = record_listings([(a, "passed"), (b, "title_kw")], now=T0)
    assert stats == RecordStats(inserted=1, updated=0, skipped_no_url=0)
    row = get_row(normalize_url(a.url))
    assert row is not None
    assert row["seen_count"] == 1
    assert row["title"] == "First wins"
    assert row["filter_verdict_last"] == "passed"


def test_empty_url_is_skipped_not_stored(seen_db):
    stats = record_listings([(_job(url=""), "passed"), (_job(url="   "), "passed")], now=T0)
    assert stats == RecordStats(inserted=0, updated=0, skipped_no_url=2)
    assert count_rows() == 0


def test_empty_batch_is_a_noop(seen_db):
    assert record_listings([], now=T0) == RecordStats(0, 0, 0)
    assert count_rows() == 0


def test_mixed_batch_counts_each_bucket(seen_db):
    known = _job(url="https://example.com/jobs/known")
    record_listings([(known, "passed")], now=T0)
    stats = record_listings(
        [
            (known, "level"),
            (_job(url="https://example.com/jobs/new"), "passed"),
            (_job(url=""), "passed"),
        ],
        now=T1,
    )
    assert stats == RecordStats(inserted=1, updated=1, skipped_no_url=1)


def test_stats_dataclass_is_frozen():
    s = RecordStats(1, 2, 3)
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.inserted = 9  # type: ignore[misc]


def test_default_now_is_utc_iso_seconds(seen_db):
    job = _job()
    before = datetime.now(timezone.utc).replace(microsecond=0)
    record_listings([(job, "passed")])
    row = get_row(normalize_url(job.url))
    assert row is not None
    seen = datetime.fromisoformat(row["first_seen"])
    assert seen.tzinfo is not None
    assert seen.utcoffset() == timedelta(0)
    assert before <= seen <= datetime.now(timezone.utc)


def test_broken_db_raises_not_swallowed(tmp_path, monkeypatch):
    """The caller's best_effort() needs the exception to count failures."""
    monkeypatch.setattr(postings_seen, "DB_PATH", tmp_path / "missing_dir" / "seen.db")
    with pytest.raises(sqlite3.Error):
        record_listings([(_job(), "passed")], now=T0)


# ── prune / read API ─────────────────────────────────────────────────────────


def test_prune_deletes_only_rows_older_than_ttl(seen_db):
    old = _job(url="https://example.com/jobs/old")
    fresh = _job(url="https://example.com/jobs/fresh")
    now = datetime(2026, 9, 13, tzinfo=timezone.utc)
    record_listings([(old, "passed")], now=now - timedelta(days=200))
    record_listings([(fresh, "passed")], now=now - timedelta(days=10))

    assert prune(180, now=now) == 1
    assert get_row(normalize_url(old.url)) is None
    assert get_row(normalize_url(fresh.url)) is not None
    assert prune(180, now=now) == 0  # idempotent


def test_prune_on_empty_db_creates_table_and_returns_zero(seen_db):
    assert prune(30) == 0
    assert count_rows() == 0


def test_get_row_round_trip_and_miss(seen_db):
    job = _job(raw=JUSTJOIN_RAW)
    record_listings([(job, "passed")], now=T0)
    row = get_row(normalize_url(job.url))
    assert row is not None
    expected = build_row(job, "passed", now=T0.isoformat(timespec="seconds"))
    assert row == expected
    assert get_row("https://example.com/nope") is None
