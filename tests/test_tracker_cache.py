"""
Tests for hunter/tracker_cache.py.

Covers: load, add, update_*, is_known_*, unsent stats, dirty tracking,
apply_pull_delta conflict matrix, concurrency.

All async methods are driven via asyncio.run() in sync test functions.
"""

import asyncio
from pathlib import Path

import openpyxl
import pytest

from hunter.tracker_cache import TrackerCache
from hunter.tracker import TRACKER_HEADERS, normalize_url, dedup_key


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


def _make_xlsx(path: Path, rows: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(TRACKER_HEADERS)
    for r in rows:
        ws.append([r.get(h, "") for h in TRACKER_HEADERS])
    wb.save(path)


# ---------------------------------------------------------------------------
# Load from Excel
# ---------------------------------------------------------------------------

class TestLoadFromExcel:
    def test_empty_when_file_missing(self, tmp_path):
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "nonexistent.xlsx"))
        assert c.size == 0
        assert c.loaded

    def test_loads_rows(self, tmp_path):
        _make_xlsx(tmp_path / "t.xlsx", [ROW_A, ROW_B])
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "t.xlsx"))
        assert c.size == 2
        assert "aaaaaaaa" in c.rows
        assert "bbbbbbbb" in c.rows

    def test_indexes_url(self, tmp_path):
        _make_xlsx(tmp_path / "t.xlsx", [ROW_A])
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "t.xlsx"))
        assert run(c.is_known_url(ROW_A["URL"]))

    def test_indexes_company_title(self, tmp_path):
        _make_xlsx(tmp_path / "t.xlsx", [ROW_A])
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "t.xlsx"))
        assert run(c.is_known_ct(ROW_A["Company"], ROW_A["Job Title"]))

    def test_skips_rows_without_id(self, tmp_path):
        row_no_id = make_row(Company="X")  # ID=""
        _make_xlsx(tmp_path / "t.xlsx", [row_no_id])
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "t.xlsx"))
        assert c.size == 0

    def test_records_sheet_row_index(self, tmp_path):
        _make_xlsx(tmp_path / "t.xlsx", [ROW_A])
        c = TrackerCache()
        run(c.load_from_excel(tmp_path / "t.xlsx"))
        # Header is row 1, first data row is row 2
        assert c.sheet_row_index.get("aaaaaaaa") == 2


# ---------------------------------------------------------------------------
# add()
# ---------------------------------------------------------------------------

class TestAdd:
    def test_add_single_row(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=5))
        assert c.size == 1
        assert run(c.is_known_url(ROW_A["URL"]))
        assert c.sheet_row_index["aaaaaaaa"] == 5

    def test_add_without_sheet_row(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        assert c.size == 1
        assert "aaaaaaaa" not in c.sheet_row_index

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

    def test_mark_dirty_and_clean(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        run(c.mark_dirty("aaaaaaaa"))
        assert "aaaaaaaa" in c.dirty_ids
        run(c.mark_clean("aaaaaaaa"))
        assert "aaaaaaaa" not in c.dirty_ids

    def test_set_sheet_row_index(self):
        c = TrackerCache()
        run(c.add(ROW_A))
        run(c.set_sheet_row_index("aaaaaaaa", 7))
        assert c.sheet_row_index["aaaaaaaa"] == 7


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

    def test_dirty_rows(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=3))
        run(c.mark_dirty("aaaaaaaa"))
        dirty = run(c.dirty_rows())
        assert len(dirty) == 1
        row_id, row, sheet_idx = dirty[0]
        assert row_id == "aaaaaaaa"
        assert sheet_idx == 3


# ---------------------------------------------------------------------------
# apply_pull_delta — conflict matrix (§9)
# ---------------------------------------------------------------------------

class TestApplyPullDelta:
    def test_no_changes_when_identical(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=2))
        to_write = run(c.apply_pull_delta([(2, dict(ROW_A))]))
        assert to_write == []

    def test_user_adds_sent_date(self):
        """Excel empty, Sheets has user date → trust Sheets, return for Excel write."""
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=2))  # Sent=""
        sheet_row = dict(ROW_A)
        sheet_row["Sent"] = "2026-05-14"
        to_write = run(c.apply_pull_delta([(2, sheet_row)]))
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == "2026-05-14"
        assert c.rows["aaaaaaaa"]["Sent"] == "2026-05-14"

    def test_bot_expired_wins_over_empty_sheets(self):
        """Excel=EXPIRED, Sheets empty → bot wins, no Excel write needed."""
        c = TrackerCache()
        row = make_row(ID="aaaaaaaa", URL="https://x.com/1",
                       Company="X", **{"Job Title": "Dev"}, Sent="EXPIRED")
        run(c.add(row, sheet_row=2))
        sheet_row = dict(row)
        sheet_row["Sent"] = ""
        to_write = run(c.apply_pull_delta([(2, sheet_row)]))
        assert to_write == []
        assert c.rows["aaaaaaaa"]["Sent"] == "EXPIRED"

    def test_user_sent_beats_expired(self):
        """Excel=EXPIRED, Sheets has user date → edge case, trust Sheets."""
        c = TrackerCache()
        row = make_row(ID="aaaaaaaa", URL="https://x.com/1",
                       Company="X", **{"Job Title": "Dev"}, Sent="EXPIRED")
        run(c.add(row, sheet_row=2))
        sheet_row = dict(row)
        sheet_row["Sent"] = "2026-05-10"
        to_write = run(c.apply_pull_delta([(2, sheet_row)]))
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == "2026-05-10"

    def test_user_erases_sent(self):
        """Excel has date, Sheets empty (user erased) → trust Sheets."""
        c = TrackerCache()
        row = make_row(ID="aaaaaaaa", URL="https://x.com/1",
                       Company="X", **{"Job Title": "Dev"}, Sent="2026-05-01")
        run(c.add(row, sheet_row=2))
        sheet_row = dict(row)
        sheet_row["Sent"] = ""
        to_write = run(c.apply_pull_delta([(2, sheet_row)]))
        assert len(to_write) == 1
        assert to_write[0]["Sent"] == ""

    def test_user_updates_to_learn(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=2))
        sheet_row = dict(ROW_A)
        sheet_row["To Learn"] = "RxJS"
        to_write = run(c.apply_pull_delta([(2, sheet_row)]))
        assert len(to_write) == 1
        assert c.rows["aaaaaaaa"]["To Learn"] == "RxJS"

    def test_updates_sheet_row_index(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=2))
        run(c.apply_pull_delta([(7, dict(ROW_A))]))
        assert c.sheet_row_index["aaaaaaaa"] == 7

    def test_missing_id_in_sheets_ignored(self):
        c = TrackerCache()
        run(c.add(ROW_A, sheet_row=2))
        to_write = run(c.apply_pull_delta([(3, make_row())]))
        assert to_write == []


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

    def test_no_deadlock_on_pull_delta(self):
        """apply_pull_delta holds lock internally — must not deadlock."""
        async def _run():
            c = TrackerCache()
            await c.add(ROW_A, sheet_row=2)
            return await asyncio.wait_for(
                c.apply_pull_delta([(2, dict(ROW_A))]),
                timeout=2.0,
            )

        result = asyncio.run(_run())
        assert result == []
