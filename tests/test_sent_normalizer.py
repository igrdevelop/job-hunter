"""
Unit tests for hunter/sent_normalizer.py — building/writing the clean date column.

All Google API calls are mocked — no network access needed.
"""

from datetime import date
from unittest.mock import MagicMock


from hunter.sent_normalizer import (
    APPLIED_COL,
    APPLIED_HEADER,
    build_column,
    normalize_sheet,
    write_column,
)


def _rows(*sent_values):
    """Build read_all-style (sheet_row_idx, row_dict) tuples from Sent values."""
    return [(i + 2, {"Sent": v}) for i, v in enumerate(sent_values)]


class TestBuildColumn:
    def test_dates_and_blanks(self):
        rows = _rows("08 04 26", "выгасла", "2026-07-04 00:00:00", "")
        grid, filled = build_column(rows)
        assert grid == [["2026-04-08"], [""], ["2026-07-04"], [""]]
        assert filled == 2

    def test_empty_rows(self):
        grid, filled = build_column([])
        assert grid == []
        assert filled == 0

    def test_order_preserved(self):
        rows = _rows("13 05", "EXPIRED", "10 04 26")
        grid, _ = build_column(rows)
        assert grid[0] == [date(date.today().year, 5, 13).isoformat()]
        assert grid[1] == [""]
        assert grid[2] == ["2026-04-10"]


class TestWriteColumn:
    def test_writes_header_and_dates(self):
        svc = MagicMock()
        grid = [["2026-04-08"], [""], ["2026-07-04"]]
        write_column(svc, "SHEET", grid, tab="Tracker")

        calls = svc.spreadsheets.return_value.values.return_value.update.call_args_list
        # First call: header into L1 (RAW); second: dates into L2:L4 (USER_ENTERED).
        header_kwargs = calls[0].kwargs
        assert header_kwargs["range"] == f"'Tracker'!{APPLIED_COL}1"
        assert header_kwargs["body"]["values"] == [[APPLIED_HEADER]]
        assert header_kwargs["valueInputOption"] == "RAW"

        data_kwargs = calls[1].kwargs
        assert data_kwargs["range"] == f"'Tracker'!{APPLIED_COL}2:{APPLIED_COL}4"
        assert data_kwargs["valueInputOption"] == "USER_ENTERED"
        assert data_kwargs["body"]["values"] == grid

    def test_empty_grid_writes_header_only(self):
        svc = MagicMock()
        write_column(svc, "SHEET", [], tab="Tracker")
        update = svc.spreadsheets.return_value.values.return_value.update
        # Only the header write happens.
        assert update.call_count == 1


class TestNormalizeSheet:
    def test_reads_then_writes(self, monkeypatch):
        svc = MagicMock()
        fake_rows = _rows("08 04 26", "повторка", "15 05")
        monkeypatch.setattr(
            "hunter.sent_normalizer.read_all",
            lambda service, sheet_id, tab="Tracker": fake_rows,
        )
        result = normalize_sheet(svc, "SHEET", tab="Tracker")
        assert result == {"rows": 3, "filled": 2}
        # write_column issued two update calls (header + data).
        assert svc.spreadsheets.return_value.values.return_value.update.call_count == 2
