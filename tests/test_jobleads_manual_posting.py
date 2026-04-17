"""JobLeads MANUAL flow: pasted job_posting.txt + tracker path resolution."""

from pathlib import Path

from hunter import tracker
from job_fetch.jobleads import JOBLEADS_PASTE_MARKER, try_load_manual_job_posting


def test_try_load_manual_job_posting_returns_none_until_paste(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    folder = tmp_path / "Applications" / "2026-04-20" / "AcmeCo"
    folder.mkdir(parents=True)
    url = "https://www.jobleads.com/pl/job/angular-dev--poland--abc123deadbeef000000000000000"

    assert tracker.add_manual_jobleads_pending(
        url=url,
        company="AcmeCo",
        title="Angular Dev",
        folder_abs=folder,
    ) is True

    jp = folder / "job_posting.txt"
    jp.write_text(
        f"URL: {url}\n\n{JOBLEADS_PASTE_MARKER}\n\nshort",
        encoding="utf-8",
    )
    assert try_load_manual_job_posting(url) is None

    body = "x" * 250
    jp.write_text(f"URL: {url}\n\n{JOBLEADS_PASTE_MARKER}\n\n{body}", encoding="utf-8")
    text = try_load_manual_job_posting(url)
    assert text is not None
    assert body in text
    assert url in text


def test_get_url_status_flags_treats_manual_as_not_success(tmp_path, monkeypatch) -> None:
    tracker_path = tmp_path / "tracker.xlsx"
    monkeypatch.setattr(tracker, "TRACKER_PATH", tracker_path)

    folder = tmp_path / "Applications" / "2026-04-20" / "Beta"
    folder.mkdir(parents=True)
    url = "https://www.jobleads.com/pl/job/fe--poland--def456deadbeef0000000000000000"
    tracker.add_manual_jobleads_pending(
        url=url, company="Beta", title="FE", folder_abs=folder,
    )
    flags = tracker.get_url_status_flags(url)
    assert flags["has_success"] is False
    assert flags["is_react_skip"] is False
