"""
hunter/postings_seen.py — one row per vacancy the hunt ever SAW.

docs/MARKET_MEMORY_PLAN.md M1: today only ``source_runs`` counts survive a
sweep — the ~90% of listings the filter/dedup rejects leave no trace, and
even an applied vacancy loses its listing ``salary``/``location``/``source``.
This table keeps the listing METADATA of every vacancy any source returned,
keyed by ``url_norm`` (the same key ``applications`` uses, so a read-side
join gives an applied row its market attributes for $0 — see the plan's M2).

What it stores, and what it does NOT
------------------------------------
* Listing attributes only (title, company, location, salary, the skills a
  JSON board lists, a filter verdict). NEVER the posting text, and
  ``text_hash`` is a digest of title+company+location+salary — not of the
  posting body. The plan forbids storing or hashing posting text for a
  vacancy the user never applied to.
* No ``user_id`` column, on purpose: a listing is the employer's public
  advertisement, not a bot user's personal data. ``hunter/erasure.py``
  discovers tables by their ``user_id`` column and correctly ignores this one.
* Deliberate additions over the plan's DDL sketch: ``salary_period`` and
  ``salary_monthly_min``/``salary_monthly_max`` keep
  ``hunter.salary_parse``'s period normalisation instead of throwing it away
  (the plan's ``salary_min``/``salary_max`` said "monthly", but the parser
  reports the string's own figures and a separate monthly pair — both are
  kept, unconverted).

Upsert semantics (``record_listings``)
--------------------------------------
A NEW ``url_norm`` inserts every column ``build_row`` computes. A KNOWN one
bumps ``last_seen``, ``seen_count`` and ``filter_verdict_last`` ONLY —
first-seen attributes are never overwritten (a salary that changed on a
re-post is a future ``last_seen``-side column, not an overwrite). One
transaction per call; a ``url_norm`` repeated within one batch counts as
seen once (first occurrence wins).

Error contract
--------------
This module MAY raise on a broken DB and deliberately does NOT swallow: the
hunt-loop caller wraps the call in ``best_effort("postings.record")``, which
needs the exception to count consecutive failures and alert at the threshold.
A bare swallow here would recreate exactly the silent-degradation class
``hunter.best_effort`` exists to close.

Storage: a ``postings_seen`` table in the same tracker.db, created lazily
(same self-contained lazy-ensure pattern as ``hunter.source_health`` /
``hunter.drive_ledger`` — NOT part of ``hunter.db.init_db()``: different
lifecycle, no ``user_id``, own TTL prune).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from hunter.config import TRACKER_DB_PATH
from hunter.db import get_db
from hunter.lang_guard import detect_posting_language
from hunter.location_parse import classify_location
from hunter.models import Job
from hunter.repost_gate import normalize_company
from hunter.salary_parse import parse_salary
from hunter.tracker import normalize_url

log = logging.getLogger(__name__)

__all__ = [
    "RecordStats",
    "build_row",
    "count_rows",
    "get_row",
    "prune",
    "record_listings",
]

# Module-level so tests can monkeypatch it onto an isolated DB (mirrors
# hunter.source_health.DB_PATH / hunter.drive_ledger.DB_PATH).
DB_PATH = TRACKER_DB_PATH

# No user_id column by design — hunter/erasure.py enumerates tables via
# their user_id column and must (and does) skip this one.
_DDL = """
CREATE TABLE IF NOT EXISTS postings_seen (
    url_norm            TEXT    PRIMARY KEY,
    url                 TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    first_seen          TEXT    NOT NULL,
    last_seen           TEXT    NOT NULL,
    seen_count          INTEGER NOT NULL DEFAULT 1,
    title               TEXT    NOT NULL DEFAULT '',
    company             TEXT    NOT NULL DEFAULT '',
    company_norm        TEXT    NOT NULL DEFAULT '',
    location_raw        TEXT    NOT NULL DEFAULT '',
    remote_mode         TEXT    NOT NULL DEFAULT '',
    city                TEXT    NOT NULL DEFAULT '',
    salary_raw          TEXT    NOT NULL DEFAULT '',
    salary_min          REAL,
    salary_max          REAL,
    salary_currency     TEXT    NOT NULL DEFAULT '',
    salary_period       TEXT    NOT NULL DEFAULT '',
    salary_contract     TEXT    NOT NULL DEFAULT '',
    salary_monthly_min  REAL,
    salary_monthly_max  REAL,
    lang                TEXT    NOT NULL DEFAULT '',
    skills_listing      TEXT    NOT NULL DEFAULT '',
    filter_verdict      TEXT    NOT NULL DEFAULT '',
    filter_verdict_last TEXT    NOT NULL DEFAULT '',
    text_hash           TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_postings_seen_last ON postings_seen(last_seen);
CREATE INDEX IF NOT EXISTS idx_postings_seen_company ON postings_seen(company_norm);
"""

# Column order of every INSERT — build_row() returns exactly these keys.
_COLUMNS: tuple[str, ...] = (
    "url_norm",
    "url",
    "source",
    "first_seen",
    "last_seen",
    "seen_count",
    "title",
    "company",
    "company_norm",
    "location_raw",
    "remote_mode",
    "city",
    "salary_raw",
    "salary_min",
    "salary_max",
    "salary_currency",
    "salary_period",
    "salary_contract",
    "salary_monthly_min",
    "salary_monthly_max",
    "lang",
    "skills_listing",
    "filter_verdict",
    "filter_verdict_last",
    "text_hash",
)

_UPSERT_SQL = (
    f"INSERT INTO postings_seen ({', '.join(_COLUMNS)}) "  # noqa: S608 — constant column names
    f"VALUES ({', '.join('?' * len(_COLUMNS))}) "
    "ON CONFLICT(url_norm) DO UPDATE SET "
    "last_seen = excluded.last_seen, "
    "seen_count = postings_seen.seen_count + 1, "
    "filter_verdict_last = excluded.filter_verdict_last"
)

# Cap on the JSON skills list; a listing with more than this is noise, not
# stack information.
MAX_SKILLS = 40

# Where the JSON boards put their listing-level skill lists (verified against
# the sources on 2026-09-13 — see _skills_from_raw). Top-level keys first,
# then dotted paths into a nested dict.
_SKILL_KEYS: tuple[str, ...] = (
    "requiredSkills",  # JustJoin (list of {name, level})
    "niceToHaveSkills",  # JustJoin
    "skills",  # JustJoin legacy flat key (now always null)
    "technologies",  # generic
    "technology",  # generic — a bare string is one entry
    "attributes",  # SmartJobs (list of {attributeName, groupName})
    "requirements.musts",  # NoFluffJobs detail shape (list of {value})
    "requirements.nices",  # NoFluffJobs detail shape
)
_SKILL_NAME_FIELDS: tuple[str, ...] = ("name", "value", "attributeName", "title")

# Chunk size for the pre-check SELECT ... IN (...): well under SQLite's
# default 999-variable ceiling on older builds.
_IN_CHUNK = 500


@dataclass(frozen=True)
class RecordStats:
    inserted: int
    updated: int
    skipped_no_url: int


def _ensure_table(conn: sqlite3.Connection) -> None:
    conn.executescript(_DDL)


def _utc_now_iso(now: datetime | None = None) -> str:
    dt = now or datetime.now(timezone.utc)
    return dt.isoformat(timespec="seconds")


# ── build_row helpers ────────────────────────────────────────────────────────


def _dig(raw: Mapping[str, Any], dotpath: str) -> Any:
    cur: Any = raw
    for part in dotpath.split("."):
        if not isinstance(cur, Mapping):
            return None
        cur = cur.get(part)
    return cur


def _skill_name(item: Any) -> str:
    """One skill entry → its name; '' when the shape carries none."""
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, Mapping):
        for field in _SKILL_NAME_FIELDS:
            val = item.get(field)
            if isinstance(val, str) and val.strip():
                return val.strip()
    return ""


def _skills_from_raw(raw: Mapping[str, Any] | None) -> list[str]:
    """Skill NAMES from a source's listing payload, deduped, capped at MAX_SKILLS.

    Handles the shapes the JSON boards actually emit — a list of strings, a
    list of dicts with a name-like key (JustJoin ``{name, level}``, SmartJobs
    ``{attributeName, groupName}``, NoFluffJobs ``{value}``), or a bare
    string — under any of ``_SKILL_KEYS``. Unknown shapes contribute nothing;
    this never raises.
    """
    if not raw or not isinstance(raw, Mapping):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for key in _SKILL_KEYS:
        val = _dig(raw, key)
        items: list[Any]
        if isinstance(val, str):
            items = [val]
        elif isinstance(val, list):
            items = val
        else:
            continue
        for item in items:
            name = _skill_name(item)
            if not name:
                continue
            fold = name.lower()
            if fold in seen:
                continue
            seen.add(fold)
            out.append(name)
            if len(out) >= MAX_SKILLS:
                return out
    return out


def _text_hash(title: str, company: str, location: str, salary: str) -> str:
    """sha1 over the folded listing attributes — NEVER over posting text."""
    parts = [(p or "").strip().lower() for p in (title, company, location, salary)]
    return hashlib.sha1("\x1f".join(parts).encode("utf-8"), usedforsecurity=False).hexdigest()


def build_row(
    job: Job,
    verdict: str,
    *,
    now: str,
    flt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Every column value for a NEW ``postings_seen`` row. Pure; no I/O.

    ``verdict`` is ``"passed"`` or one of ``hunter.filters.FILTER_REASONS`` —
    the caller decides, this only stores it (in both ``filter_verdict`` and
    ``filter_verdict_last``, since on a new row they coincide). ``lang`` comes
    from ``detect_posting_language(job.title)``, which is built for full
    postings and defaults to EN on a short title — stored as best-effort.
    """
    title = (job.title or "").strip()
    company = (job.company or "").strip()
    location = (job.location or "").strip()
    salary_raw = (job.salary or "").strip()

    loc = classify_location(location, flt=flt)
    sal = parse_salary(salary_raw)
    skills = _skills_from_raw(job.raw)

    return {
        "url_norm": normalize_url(job.url or ""),
        "url": (job.url or "").strip(),
        "source": (job.source or "").strip(),
        "first_seen": now,
        "last_seen": now,
        "seen_count": 1,
        "title": title,
        "company": company,
        "company_norm": normalize_company(company),
        "location_raw": location,
        "remote_mode": loc.remote_mode,
        "city": loc.city,
        "salary_raw": sal.raw,
        "salary_min": sal.min,
        "salary_max": sal.max,
        "salary_currency": sal.currency,
        "salary_period": sal.period,
        "salary_contract": sal.contract,
        "salary_monthly_min": sal.monthly_min,
        "salary_monthly_max": sal.monthly_max,
        "lang": detect_posting_language(title),
        "skills_listing": json.dumps(skills, ensure_ascii=False) if skills else "",
        "filter_verdict": verdict or "",
        "filter_verdict_last": verdict or "",
        "text_hash": _text_hash(title, company, location, salary_raw),
    }


# ── Public write API ─────────────────────────────────────────────────────────


def _existing_url_norms(conn: sqlite3.Connection, url_norms: list[str]) -> set[str]:
    found: set[str] = set()
    for i in range(0, len(url_norms), _IN_CHUNK):
        chunk = url_norms[i : i + _IN_CHUNK]
        placeholders = ", ".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT url_norm FROM postings_seen WHERE url_norm IN ({placeholders})",  # noqa: S608
            chunk,
        ).fetchall()
        found.update(r["url_norm"] for r in rows)
    return found


def record_listings(
    items: Iterable[tuple[Job, str]],
    *,
    now: datetime | None = None,
    flt: Mapping[str, Any] | None = None,
) -> RecordStats:
    """Upsert one sweep's ``(job, verdict)`` pairs in ONE transaction.

    New ``url_norm`` → full row; known one → ``last_seen`` / ``seen_count`` /
    ``filter_verdict_last`` only. A ``url_norm`` repeated within the batch is
    recorded once (first occurrence wins). A job whose URL normalises to ''
    is counted in ``skipped_no_url`` and not stored.

    Raises on a broken DB — see the module docstring.
    """
    ts = _utc_now_iso(now)
    rows: list[dict[str, Any]] = []
    seen_in_batch: set[str] = set()
    skipped_no_url = 0
    for job, verdict in items:
        url_norm = normalize_url(job.url or "")
        if not url_norm:
            skipped_no_url += 1
            continue
        if url_norm in seen_in_batch:
            continue
        seen_in_batch.add(url_norm)
        rows.append(build_row(job, verdict, now=ts, flt=flt))

    inserted = updated = 0
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        if rows:
            # The upsert's rowcount can't tell an insert from an update, so
            # look the keys up first — same transaction, so nothing can slip
            # in between.
            existing = _existing_url_norms(conn, [r["url_norm"] for r in rows])
            updated = sum(1 for r in rows if r["url_norm"] in existing)
            inserted = len(rows) - updated
            conn.executemany(_UPSERT_SQL, [tuple(r[c] for c in _COLUMNS) for r in rows])

    log.info(
        "postings_seen: inserted=%d updated=%d skipped_no_url=%d",
        inserted,
        updated,
        skipped_no_url,
    )
    return RecordStats(inserted=inserted, updated=updated, skipped_no_url=skipped_no_url)


def prune(ttl_days: int, *, now: datetime | None = None) -> int:
    """Delete rows whose ``last_seen`` is older than ``ttl_days``; return the count."""
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=ttl_days)
    cutoff_iso = cutoff.isoformat(timespec="seconds")
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        cur = conn.execute("DELETE FROM postings_seen WHERE last_seen < ?", (cutoff_iso,))
        deleted = cur.rowcount if cur.rowcount is not None and cur.rowcount >= 0 else 0
    if deleted:
        log.info("postings_seen: pruned %d rows older than %d days", deleted, ttl_days)
    return deleted


# ── Public read API ──────────────────────────────────────────────────────────


def count_rows() -> int:
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT COUNT(*) AS n FROM postings_seen").fetchone()
    return int(row["n"]) if row else 0


def get_row(url_norm: str) -> dict[str, Any] | None:
    """The stored row for ``url_norm`` as a plain dict, or None."""
    with get_db(DB_PATH) as conn:
        _ensure_table(conn)
        row = conn.execute("SELECT * FROM postings_seen WHERE url_norm = ?", (url_norm,)).fetchone()
    return dict(row) if row else None
