"""
Unit tests for hunter/gsheets_client.py.

All Google API calls are mocked — no network access needed.
"""

import pytest
from unittest.mock import MagicMock, patch, call

from hunter.gsheets_client import (
    COLUMNS,
    _list_to_row,
    _parse_start_row,
    _range,
    _row_to_list,
    append_rows,
    batch_write_all,
    create_spreadsheet,
    read_all,
    update_cell,
    update_row,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def mock_service():
    """Return a MagicMock that chains .spreadsheets().values().xxx().execute()."""
    svc = MagicMock()
    return svc


SAMPLE_ROW = {
    "Date": "2026-05-14",
    "Company": "Acme",
    "Job Title": "Senior Frontend",
    "Stack": "Angular",
    "ATS %": "85",
    "URL": "https://example.com/job/1",
    "Folder": "/app/Applications/Acme",
    "Sent": "",
    "Re-application": "",
    "To Learn": "",
    "ID": "abcd1234",
}


# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

class TestRowHelpers:
    def test_row_to_list_full(self):
        lst = _row_to_list(SAMPLE_ROW)
        assert lst[0] == "2026-05-14"
        assert lst[1] == "Acme"
        assert lst[10] == "abcd1234"
        assert len(lst) == 11

    def test_row_to_list_missing_keys(self):
        lst = _row_to_list({"Company": "X"})
        assert lst[1] == "X"
        assert all(v == "" for i, v in enumerate(lst) if i != 1)

    def test_row_to_list_none_values(self):
        lst = _row_to_list({"Company": None, "ID": "abc"})
        assert lst[1] == ""
        assert lst[10] == "abc"

    def test_list_to_row_full(self):
        lst = ["2026-05-14", "Acme", "SFE", "Angular", "85",
               "https://x.com", "/app", "", "", "", "abcd1234"]
        row = _list_to_row(lst)
        assert row["Company"] == "Acme"
        assert row["ID"] == "abcd1234"

    def test_list_to_row_short(self):
        """Short lists (API sometimes omits trailing empty cells) get padded."""
        row = _list_to_row(["2026-05-14", "Acme"])
        assert row["ID"] == ""
        assert len(row) == 11

    def test_roundtrip(self):
        lst = _row_to_list(SAMPLE_ROW)
        recovered = _list_to_row(lst)
        for col in COLUMNS:
            assert recovered[col] == SAMPLE_ROW.get(col, "")

    def test_parse_start_row(self):
        assert _parse_start_row("Tracker!A5:K7") == 5
        assert _parse_start_row("'Tracker'!A2:K2") == 2

    def test_parse_start_row_bad_input(self):
        assert _parse_start_row("") == -1
        assert _parse_start_row("bad") == -1

    def test_range_full_tab(self):
        assert _range("Tracker") == "'Tracker'!A:K"

    def test_range_single_row(self):
        assert _range("Tracker", 5) == "'Tracker'!A5:K5"

    def test_range_row_span(self):
        assert _range("Tracker", 2, 10) == "'Tracker'!A2:K10"


# ---------------------------------------------------------------------------
# read_all
# ---------------------------------------------------------------------------

class TestReadAll:
    def test_returns_empty_for_no_data(self):
        svc = mock_service()
        svc.spreadsheets().values().get().execute.return_value = {}
        result = read_all(svc, "sheet123")
        assert result == []

    def test_skips_header_row(self):
        svc = mock_service()
        header = COLUMNS
        data_row = ["2026-05-14", "Acme", "SFE", "Angular", "85",
                    "https://x.com", "/app", "", "", "", "abcd1234"]
        svc.spreadsheets().values().get().execute.return_value = {
            "values": [header, data_row]
        }
        result = read_all(svc, "sheet123")
        assert len(result) == 1
        row_idx, row = result[0]
        assert row_idx == 2  # sheet row 2 (after header)
        assert row["Company"] == "Acme"
        assert row["ID"] == "abcd1234"

    def test_multiple_rows(self):
        svc = mock_service()
        header = COLUMNS
        rows = [
            ["2026-05-14", f"Co{i}", "Dev", "JS", "70",
             f"https://x.com/{i}", "", "", "", "", f"id{i:08x}"]
            for i in range(3)
        ]
        svc.spreadsheets().values().get().execute.return_value = {
            "values": [header] + rows
        }
        result = read_all(svc, "sheet123")
        assert len(result) == 3
        assert result[0][0] == 2
        assert result[1][0] == 3
        assert result[2][0] == 4
        assert result[0][1]["Company"] == "Co0"

    def test_header_only_returns_empty(self):
        svc = mock_service()
        svc.spreadsheets().values().get().execute.return_value = {
            "values": [COLUMNS]
        }
        result = read_all(svc, "sheet123")
        assert result == []


# ---------------------------------------------------------------------------
# append_rows
# ---------------------------------------------------------------------------

class TestAppendRows:
    def test_empty_list_returns_empty(self):
        svc = mock_service()
        result = append_rows(svc, "sheet123", [])
        assert result == []
        svc.spreadsheets().values().append.assert_not_called()

    def test_single_row(self):
        svc = mock_service()
        svc.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "Tracker!A5:K5"}
        }
        result = append_rows(svc, "sheet123", [SAMPLE_ROW])
        assert result == [5]

    def test_multiple_rows(self):
        svc = mock_service()
        svc.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "Tracker!A5:K7"}
        }
        rows = [SAMPLE_ROW, SAMPLE_ROW, SAMPLE_ROW]
        result = append_rows(svc, "sheet123", rows)
        assert result == [5, 6, 7]

    def test_correct_api_params(self):
        svc = mock_service()
        svc.spreadsheets().values().append().execute.return_value = {
            "updates": {"updatedRange": "Tracker!A2:K2"}
        }
        append_rows(svc, "sheet123", [SAMPLE_ROW])
        # call_args reflects the real call made by append_rows
        call_kwargs = svc.spreadsheets().values().append.call_args.kwargs
        assert call_kwargs["valueInputOption"] == "RAW"
        assert call_kwargs["insertDataOption"] == "INSERT_ROWS"


# ---------------------------------------------------------------------------
# update_row
# ---------------------------------------------------------------------------

class TestUpdateRow:
    def test_calls_update_with_correct_range(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        update_row(svc, "sheet123", 5, SAMPLE_ROW)
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        assert "A5:K5" in call_kwargs["range"]

    def test_row_values_passed(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        update_row(svc, "sheet123", 3, SAMPLE_ROW)
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        assert call_kwargs["body"]["values"][0][1] == "Acme"


# ---------------------------------------------------------------------------
# update_cell
# ---------------------------------------------------------------------------

class TestUpdateCell:
    def test_correct_cell_range(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        update_cell(svc, "sheet123", 5, "Sent", "2026-05-14")
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        # Sent is column H (index 7)
        assert "H5" in call_kwargs["range"]
        assert call_kwargs["body"]["values"] == [["2026-05-14"]]

    def test_id_column(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        update_cell(svc, "sheet123", 2, "ID", "abcd1234")
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        # ID is column K (index 10)
        assert "K2" in call_kwargs["range"]

    def test_invalid_column_raises(self):
        svc = mock_service()
        with pytest.raises(ValueError, match="Unknown column"):
            update_cell(svc, "sheet123", 5, "NonExistent", "value")

    def test_all_columns_have_correct_letter(self):
        """Verify every column maps to the expected letter."""
        expected = dict(zip(COLUMNS, "ABCDEFGHIJK"))
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        for col, expected_letter in expected.items():
            update_cell(svc, "sheet123", 1, col, "x")
            call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
            assert f"{expected_letter}1" in call_kwargs["range"], \
                f"Column {col!r} should map to letter {expected_letter!r}"


# ---------------------------------------------------------------------------
# create_spreadsheet
# ---------------------------------------------------------------------------

class TestCreateSpreadsheet:
    def _setup_create_mock(self, svc):
        svc.spreadsheets().create().execute.return_value = {
            "spreadsheetId": "newsheet123",
            "sheets": [{"properties": {"sheetId": 0, "title": "Tracker"}}],
        }
        svc.spreadsheets().values().update().execute.return_value = {}
        svc.spreadsheets().batchUpdate().execute.return_value = {}

    def test_returns_spreadsheet_id(self):
        svc = mock_service()
        self._setup_create_mock(svc)
        result = create_spreadsheet(svc, "Job Tracker")
        assert result == "newsheet123"

    def test_writes_header_row(self):
        svc = mock_service()
        self._setup_create_mock(svc)
        create_spreadsheet(svc, "Job Tracker")
        update_calls = svc.spreadsheets().values().update.call_args_list
        header_call = next(
            c for c in update_calls
            if "'Tracker'!A1:K1" in str(c)
        )
        assert header_call.kwargs["body"]["values"] == [COLUMNS]

    def test_formats_header_bold(self):
        svc = mock_service()
        self._setup_create_mock(svc)
        create_spreadsheet(svc, "Job Tracker")
        batch_call = svc.spreadsheets().batchUpdate.call_args
        requests = batch_call.kwargs["body"]["requests"]
        cell_formats = [r for r in requests if "repeatCell" in r]
        assert cell_formats, "Expected a repeatCell formatting request"
        fmt = cell_formats[0]["repeatCell"]["cell"]["userEnteredFormat"]
        assert fmt["textFormat"]["bold"] is True

    def test_freezes_header_row(self):
        svc = mock_service()
        self._setup_create_mock(svc)
        create_spreadsheet(svc, "Job Tracker")
        batch_call = svc.spreadsheets().batchUpdate.call_args
        requests = batch_call.kwargs["body"]["requests"]
        freeze_reqs = [r for r in requests if "updateSheetProperties" in r]
        assert freeze_reqs
        frozen = freeze_reqs[0]["updateSheetProperties"]["properties"]["gridProperties"]
        assert frozen["frozenRowCount"] == 1


# ---------------------------------------------------------------------------
# batch_write_all
# ---------------------------------------------------------------------------

class TestBatchWriteAll:
    def test_empty_rows_noop(self):
        svc = mock_service()
        batch_write_all(svc, "sheet123", [])
        svc.spreadsheets().values().update.assert_not_called()

    def test_writes_correct_range(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        rows = [SAMPLE_ROW, SAMPLE_ROW]
        batch_write_all(svc, "sheet123", rows)
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        # 2 rows -> A2:K3
        assert call_kwargs["range"] == "'Tracker'!A2:K3"

    def test_passes_correct_values(self):
        svc = mock_service()
        svc.spreadsheets().values().update().execute.return_value = {}
        batch_write_all(svc, "sheet123", [SAMPLE_ROW])
        call_kwargs = svc.spreadsheets().values().update.call_args.kwargs
        values = call_kwargs["body"]["values"]
        assert len(values) == 1
        assert values[0][1] == "Acme"
        assert values[0][10] == "abcd1234"
