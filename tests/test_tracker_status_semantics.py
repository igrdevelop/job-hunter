import openpyxl

from hunter import tracker


def _append_row(ws, *, company: str, title: str, ats: str, url: str, sent: str = "") -> None:
    row = [
        "2026-04-16",  # Date
        company,       # Company
        title,         # Job Title
        "Angular",     # Stack
        ats,           # ATS % / status
        url,           # URL
        "",            # Folder
        sent,          # Sent
        "",            # Re-application
        "",            # To Learn
    ]
    ws.append(row)


def test_get_url_status_flags_detects_success(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    wb, ws = tracker._load_or_create()
    _append_row(
        ws,
        company="Acme",
        title="Senior Frontend Developer",
        ats="82%",
        url="https://example.com/jobs/1?utm_source=mail",
    )
    wb.save(tracker_path)
    wb.close()

    flags = tracker.get_url_status_flags("https://example.com/jobs/1")
    assert flags == {"has_success": True, "is_react_skip": False}


def test_get_url_status_flags_detects_react_skip(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    wb, ws = tracker._load_or_create()
    _append_row(
        ws,
        company="Acme",
        title="Frontend Developer",
        ats="SKIP",
        url="https://example.com/jobs/2",
        sent="—",
    )
    wb.save(tracker_path)
    wb.close()

    flags = tracker.get_url_status_flags("https://example.com/jobs/2")
    assert flags == {"has_success": False, "is_react_skip": True}


def test_get_url_status_flags_ignores_fail_and_plain_skip(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    wb, ws = tracker._load_or_create()
    _append_row(
        ws,
        company="Acme",
        title="Frontend Developer",
        ats="FAIL",
        url="https://example.com/jobs/3",
    )
    _append_row(
        ws,
        company="Acme",
        title="Frontend Developer",
        ats="SKIP",
        url="https://example.com/jobs/3",
        sent="",
    )
    wb.save(tracker_path)
    wb.close()

    flags = tracker.get_url_status_flags("https://example.com/jobs/3")
    assert flags == {"has_success": False, "is_react_skip": False}


def test_get_url_status_flags_is_case_insensitive_for_status_values(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    wb, ws = tracker._load_or_create()
    _append_row(
        ws,
        company="Acme",
        title="Frontend Developer",
        ats="skip",
        url="https://example.com/jobs/4",
        sent="—",
    )
    _append_row(
        ws,
        company="Acme",
        title="Frontend Developer",
        ats="fail",
        url="https://example.com/jobs/5",
        sent="",
    )
    wb.save(tracker_path)
    wb.close()

    flags_skip = tracker.get_url_status_flags("https://example.com/jobs/4")
    flags_fail = tracker.get_url_status_flags("https://example.com/jobs/5")
    assert flags_skip == {"has_success": False, "is_react_skip": True}
    assert flags_fail == {"has_success": False, "is_react_skip": False}
