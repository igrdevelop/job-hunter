from pathlib import Path

from hunter.services.tracker_service import record_successful_apply, should_skip_url


def _content(url: str) -> dict:
    return {
        "company_name": "Acme",
        "job_title": "Senior Frontend Developer",
        "stack": "Angular",
        "ats_score": "85",
        "apply_url": url,
        "output_folder": str(Path("/tmp") / "Acme"),
        "to_learn": "State management",
    }


def test_should_skip_url_false_for_unknown_url(tracker_db) -> None:
    assert should_skip_url("https://example.com/jobs/1") is False


def test_record_successful_apply_writes_and_enables_skip(tracker_db) -> None:
    assert record_successful_apply(_content("https://example.com/jobs/2"), force=False) is True
    assert should_skip_url("https://example.com/jobs/2") is True
