import uuid

import hunter.db as db_module
from hunter import tracker


def _row_dict(*, company: str, title: str, ats: str, url: str, sent: str = "") -> dict:
    return {
        "ID": uuid.uuid4().hex[:8],
        "Date": "2026-04-16",
        "Company": company,
        "Job Title": title,
        "Stack": "Angular",
        "ATS %": ats,
        "URL": url,
        "Folder": "",
        "Sent": sent,
        "Re-application": "",
        "To Learn": "",
    }


def test_get_url_status_flags_detects_success() -> None:
    db_module.insert_job(_row_dict(
        company="Acme", title="Senior Frontend Developer",
        ats="82%", url="https://example.com/jobs/1?utm_source=mail",
    ))
    flags = tracker.get_url_status_flags("https://example.com/jobs/1")
    assert flags == {"has_success": True, "is_react_skip": False}


def test_get_url_status_flags_detects_react_skip() -> None:
    db_module.insert_job(_row_dict(
        company="Acme", title="Frontend Developer",
        ats="SKIP", url="https://example.com/jobs/2", sent="—",
    ))
    flags = tracker.get_url_status_flags("https://example.com/jobs/2")
    assert flags == {"has_success": False, "is_react_skip": True}


def test_get_url_status_flags_ignores_fail_and_plain_skip() -> None:
    db_module.insert_job(_row_dict(
        company="Acme", title="Frontend Developer",
        ats="FAIL", url="https://example.com/jobs/3",
    ))
    db_module.insert_job(_row_dict(
        company="Acme", title="Frontend Developer",
        ats="SKIP", url="https://example.com/jobs/3",
        sent="",
    ), replace=True)
    flags = tracker.get_url_status_flags("https://example.com/jobs/3")
    assert flags == {"has_success": False, "is_react_skip": False}


def test_get_url_status_flags_is_case_insensitive_for_status_values() -> None:
    db_module.insert_job(_row_dict(
        company="Acme", title="Frontend Developer",
        ats="skip", url="https://example.com/jobs/4", sent="—",
    ))
    db_module.insert_job(_row_dict(
        company="Acme", title="Frontend Developer",
        ats="fail", url="https://example.com/jobs/5", sent="",
    ))
    flags_skip = tracker.get_url_status_flags("https://example.com/jobs/4")
    flags_fail = tracker.get_url_status_flags("https://example.com/jobs/5")
    assert flags_skip == {"has_success": False, "is_react_skip": True}
    assert flags_fail == {"has_success": False, "is_react_skip": False}
