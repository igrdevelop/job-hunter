"""
Tests for hunter/tracker_cache.py.

Covers: load, add, update_*, is_known_*, unsent stats, dirty tracking,
apply_pull_delta conflict matrix, concurrency.

All async methods are driven via asyncio.run() in sync test functions.
"""

import asyncio
import uuid
from pathlib import Path

import pytest

from hunter.tracker_cache import TrackerCache
from hunter.tracker import TRACKER_HEADERS, normalize_url
from hunter.db import get_db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(coro):
    return asyncio.run(coro)


def make_row(**kwargs) -> dict:
    base = {h: "" for h in TRACKER_HEADERS}
    base.update(kwargs)
    return base


ROW_A = make_row(
    ID="aaaaaaaa",
    Company="Acme",
    **{"Job Title": "Senior Angular Dev"},
    URL="https://example.com/jobs/1",
    **{"ATS %": "85"},
    Sent="",
    Stack="Angular",
)

ROW_B = make_row(
    ID="bbbbbbbb",
    Company="Beta Corp",
    **{"Job Title": "Frontend Engineer"},
    URL="https://beta.io/jobs/99",
    **{"ATS %": "72"},
    Sent="2026-05-01",
    Stack="React",
)

ROW_C = make_row(
    ID="cccccccc",
    Company="Gamma",
    **{"Job Title": "Angular Developer"},
    URL="https://gamma.io/jobs/3",
    **{"ATS %": "SKIP"},
    Sent="",
    Stack="Angular, TypeScript",
)


def _insert_row(tracker_db: Path, row: dict) -> None:
    """Insert a tracker row dict directly into the test SQLite DB."""
    from hunter.tracker import normalize_url
    row_id = row.get("ID") or uuid.uuid4().hex[:8]
    url = row.get("URL", "")
    with get_db(tracker_db) as conn:
        conn.execute(
            """
            INSERT INTO applications
            (id, date, company, title, stack, ats_status, url, url_norm,
             folder, sent, reapplication, to_learn, drive_url, confirmation, answer)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                row_id,
                row.get("Date", ""),
                row.get("Company", ""),
                row.get("Job Title", ""),
                row.get("Stack", ""),
                row.get("ATS %", ""),
                url,
                normalize_url(url),
                row.get("Folder", ""),
                row.get("Sent", ""),
                row.get("Re-application", ""),
                row.get("To Learn", ""),
                row.get("Drive URL", ""),
                row.get("Confirmation", ""),
                row.get("Answer", ""),
            ),
        )


# ---------------------------------------------------------------------------
# Load from DB
# ---------------------------------------------------------------------------

class TestLoadFromDB:
    def test_empty_when_db_empty(self, tracker_db):
        c = TrackerCache()
        run(c.load_from_db())
        assert c.size == 0
        assert c.loaded

    def test_loads_rows(self, tracker_db):
        _insert_row(tracker_db, ROW_A)
        _insert_row(tracker_db, ROW_B)
        c = TrackerCache()
        run(c.load_from_db())
        assert c.size == 2
        assert "aaaaaaaa" in c.rows
        assert "bbbbbbbb" in c.rows

    def test_indexes_url(self, tracker_db):
        _insert_row(tracker_db, ROW_A)
        c = TrackerCache()
        run(c.load_from_db())
        assert run(c.is_known_url(ROW_A["URL"]))

    def test_indexes_company_title(self, tracker_db):
        _insert_row(tracker_db, ROW_A)
        c = TrackerCache()
        run(c.load_from_db())
        assert run(c.is_known_ct(ROW_A["Company"], ROW_A["Job Title"]))

    def test_load_from_excel_deprecated_wrapper(self, tracker_db):
        """load_from_excel() still works (deprecated alias for load_from_db())."""
        _insert_row(tracker_db, ROW_A)
        c = TrackerCache()
        run(c.load_from_excel())   # no path needed — reads from DB
        assert c.size == 1


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_single_row(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        assert c.size == 1
        assert run(c.is_known_url(ROW_A["URL"]))

    def test_add_missing_id_skipped(self):
        c = TrackerCache()
        run(c.add(make_row(Company="X")))
        assert c.size == 0

    def test_add_updates_by_url_to_latest(self):
        url = "https://example.com/jobs/1"
        c = TrackerCache()
        row1 = make_row(ID="id000001", URL=url, Company="X", **{"Job Title": "Dev"})
        row2 = make_row(ID="id000002", URL=url, Company="X", **{"Job Title": "Dev"})
        run(c.add(row1))
        run(c.add(row2))
        assert c.by_url[normalize_url(url)] == "id000002"


# ---------------------------------------------------------------------------
# update_*
# ---------------------------------------------------------------------------

class TestUpdate:
    def test_update_status(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        run(c.update_status("aaaaaaaa", "SKIP"))
        assert c.rows["aaaaaaaa"]["ATS %"] == "SKIP"

    def test_update_status_unknown_id_no_raise(self):
        c = TrackerCache()
        run(c.update_status("nosuchid", "SKIP"))  # should just log, not raise

    def test_update_sent(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        run(c.update_sent("aaaaaaaa", "2026-05-14"))
        assert c.rows["aaaaaaaa"]["Sent"] == "2026-05-14"

    def test_update_field_known(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        run(c.update_field("aaaaaaaa", "To Learn", "RxJS"))
        assert c.rows["aaaaaaaa"]["To Learn"] == "RxJS"

    def test_update_field_invalid_raises(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        with pytest.raises(ValueError, match="Unknown field"):
            run(c.update_field("aaaaaaaa", "NonExistent", "x"))


# ---------------------------------------------------------------------------
# Dedup reads
# ---------------------------------------------------------------------------

class TestDedup:
    def test_is_known_url_false_when_empty(self):
        c = TrackerCache()
        assert not run(c.is_known_url("https://example.com/jobs/1"))

    def test_is_known_url_true_after_add(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        assert run(c.is_known_url("https://example.com/jobs/1"))

    def test_is_known_url_normalized(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        assert run(c.is_known_url("https://example.com/jobs/1/?utm_source=foo"))

    def test_is_known_ct_false_when_empty(self):
        c = TrackerCache()
        assert not run(c.is_known_ct("Acme", "Senior Angular Dev"))

    def test_is_known_ct_true_after_add(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        assert run(c.is_known_ct("Acme", "Senior Angular Dev"))

    def test_get_row_by_url(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        row = run(c.get_row_by_url(ROW_A["URL"]))
        assert row is not None
        assert row["ID"] == "aaaaaaaa"

    def test_get_row_by_url_missing(self):
        c = TrackerCache()
        assert run(c.get_row_by_url("https://nothere.com")) is None


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class TestStats:
    def test_unsent_count(self):
        c = TrackerCache()
        run(c.add(ROW_A))  # Sent=""
        run(c.add(ROW_B))  # Sent="2026-05-01"
        run(c.add(ROW_C))  # Sent=""
        assert run(c.unsent_count()) == 2

    def test_unsent_angular_count(self):
        c = TrackerCache()
        run(c.add(ROW_A))  # unsent, Stack=Angular
        run(c.add(ROW_B))  # sent, Stack=React
        run(c.add(ROW_C))  # unsent, Stack=Angular
        assert run(c.unsent_angular_count()) == 2

    def test_unsent_angular_count_title_match(self):
        c = TrackerCache()
        row = make_row(ID="dddddddd", **{"Job Title": "Angular Engineer"}, Stack="TS", Sent="")
        run(c.add(row))
        assert run(c.unsent_angular_count()) == 1

    def test_all_unsent(self):
        c = TrackerCache()
        run(c.add(ROW_A))  # unsent
        run(c.add(ROW_B))  # sent
        unsent = run(c.all_unsent())
        assert len(unsent) == 1
        assert unsent[0]["ID"] == "aaaaaaaa"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

class TestConcurrency:
    def test_concurrent_adds_no_race(self):
        """100 concurrent adds should all land without corrupting indexes."""
        rows = [
            make_row(ID=f"{i:08x}", URL=f"https://x.com/{i}",
                     Company=f"Co{i}", **{"Job Title": "Dev"})
            for i in range(100)
        ]

        async def _run():
            c = TrackerCache()
            await asyncio.gather(*[c.add(r) for r in rows])
            return c

        c = asyncio.run(_run())
        assert c.size == 100
        assert len(c.by_url) == 100

    def test_concurrent_reads_and_writes(self):
        """Reads and writes can run concurrently without deadlock."""
        async def _run():
            c = TrackerCache()
            await c.add(ROW_A)

            async def writer(n):
                for _ in range(n):
                    await c.update_status("aaaaaaaa", "SKIP")
                    await c.update_sent("aaaaaaaa", "2026-05-14")

            async def reader(n):
                for _ in range(n):
                    await c.is_known_url(ROW_A["URL"])
                    await c.unsent_count()

            await asyncio.gather(writer(50), reader(50), writer(50), reader(50))
            return c

        c = asyncio.run(_run())
        assert c.rows["aaaaaaaa"]["ATS %"] == "SKIP"

