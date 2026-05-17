from pathlib import Path

from hunter.services.tracker_service import record_successful_apply, should_skip_url


def _content(url: str, folder: Path) -> dict:
    return {
        "company_name": "Acme",
        "job_title": "Senior Frontend Developer",
        "stack": "Angular",
        "ats_score": "85",
        "apply_url": url,
        "output_folder": str(folder),
        "to_learn": "State management",
    }


def test_should_skip_url_false_for_unknown_url(tmp_path, monkeypatch) -> None:
    from hunter import tracker

    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    assert should_skip_url("https://example.com/jobs/1") is False


def test_record_successful_apply_writes_and_enables_skip(tmp_path, monkeypatch) -> None:
    from hunter import tracker

    monkeypatch.setattr(tracker, "TRACKER_PATH", tmp_path / "tracker.xlsx")
    content = _content("https://example.com/jobs/2", tmp_path / "Applications" / "2026-04-16" / "Acme")

    assert record_successful_apply(content, force=False) is True
    assert should_skip_url("https://example.com/jobs/2") is True
