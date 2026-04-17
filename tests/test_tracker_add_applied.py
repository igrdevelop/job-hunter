from pathlib import Path

import openpyxl

from hunter import tracker


def _build_content(url: str, output_folder: Path) -> dict:
    return {
        "company_name": "Acme",
        "job_title": "Senior Frontend Developer",
        "stack": "Angular",
        "ats_score": "85",
        "apply_url": url,
        "output_folder": str(output_folder),
        "to_learn": "State management",
    }


def test_add_applied_writes_success_row(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    content = _build_content(
        "https://example.com/jobs/1?utm_source=mail",
        tmp_path / "Applications" / "2026-04-16" / "Acme",
    )
    written = tracker.add_applied(content, force=False)

    assert written is True
    assert tracker.has_successful_entry("https://example.com/jobs/1")

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()
    assert len(rows) == 1
    assert rows[0][1] == "Acme"
    assert rows[0][2] == "Senior Frontend Developer"
    assert rows[0][4] == "85%"


def test_add_applied_skips_duplicate_success_when_not_forced(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    content = _build_content(
        "https://example.com/jobs/2?utm_source=mail",
        tmp_path / "Applications" / "2026-04-16" / "Acme",
    )
    assert tracker.add_applied(content, force=False) is True
    assert tracker.add_applied(content, force=False) is False

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()
    assert len(rows) == 1


def test_add_applied_marks_reapplication_when_forced(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    content = _build_content(
        "https://example.com/jobs/3",
        tmp_path / "Applications" / "2026-04-16" / "Acme",
    )
    assert tracker.add_applied(content, force=False) is True
    assert tracker.add_applied(content, force=True) is True

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    assert len(rows) == 2
    # Re-application column should be marked on the second row.
    assert rows[1][8] == "+"


def test_add_applied_accepts_non_numeric_ats_score(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    content = _build_content(
        "https://example.com/jobs/4",
        tmp_path / "Applications" / "2026-04-16" / "Acme",
    )
    content["ats_score"] = "N/A"

    assert tracker.add_applied(content, force=False) is True

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    assert len(rows) == 1
    assert rows[0][4] == "N/A"


def test_add_applied_removes_manual_pending_row_first(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    folder = tmp_path / "Applications" / "2026-04-21" / "GammaInc"
    folder.mkdir(parents=True)
    url = "https://www.jobleads.com/pl/job/x--poland--aaa111deadbeef0000000000000000"
    assert tracker.add_manual_jobleads_pending(
        url=url, company="GammaInc", title="Dev", folder_abs=folder,
    ) is True

    content = _build_content(url, folder)
    assert tracker.add_applied(content, force=False) is True

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = [tuple(r) for r in ws.iter_rows(min_row=2, values_only=True)]
    wb.close()
    assert len(rows) == 1
    assert rows[0][4] == "85%"
    assert rows[0][5] == url


def test_add_applied_converts_10_point_scale_to_percent(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    content = _build_content(
        "https://example.com/jobs/5",
        tmp_path / "Applications" / "2026-04-16" / "Acme",
    )
    content["ats_score"] = "8/10"

    assert tracker.add_applied(content, force=False) is True

    wb = openpyxl.load_workbook(tracker_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    assert len(rows) == 1
    assert rows[0][4] == "80%"
