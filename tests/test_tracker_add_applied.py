"""Tests for tracker.add_applied, add_manual_jobleads_pending, apply_pull_updates."""

from pathlib import Path

from hunter import tracker
from hunter.tracker import add_applied, apply_pull_updates, lookup_url


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


def test_add_applied_writes_success_row(tracker_db) -> None:
    content = _build_content(
        "https://example.com/jobs/1?utm_source=mail",
        Path("/tmp") / "Applications" / "2026-04-16" / "Acme",
    )
    written = tracker.add_applied(content, force=False)

    assert written is True
    assert tracker.has_successful_entry("https://example.com/jobs/1")

    rows = lookup_url("https://example.com/jobs/1")
    assert len(rows) == 1
    assert rows[0]["company"] == "Acme"
    assert rows[0]["title"] == "Senior Frontend Developer"
    assert rows[0]["ats"] == "85%"


def test_add_applied_skips_duplicate_success_when_not_forced(tracker_db) -> None:
    content = _build_content(
        "https://example.com/jobs/2?utm_source=mail",
        Path("/tmp") / "Applications" / "2026-04-16" / "Acme",
    )
    assert tracker.add_applied(content, force=False) is True
    assert tracker.add_applied(content, force=False) is False

    rows = lookup_url("https://example.com/jobs/2")
    assert len(rows) == 1


def test_add_applied_marks_reapplication_when_forced(tracker_db) -> None:
    # force=True replaces the old row (DELETE + INSERT) to prevent duplicates.
    # The reapplication flag is still set because is_reapply is checked before
    # the delete, so we correctly detect the prior entry.
    content = _build_content(
        "https://example.com/jobs/3",
        Path("/tmp") / "Applications" / "2026-04-16" / "Acme",
    )
    assert tracker.add_applied(content, force=False) is True
    assert tracker.add_applied(content, force=True) is True

    from hunter.db import get_db

    with get_db(tracker_db) as conn:
        rows = conn.execute(
            "SELECT reapplication FROM applications WHERE url_norm LIKE '%example.com/jobs/3%'"
        ).fetchall()
    assert len(rows) == 1
    assert rows[0]["reapplication"] == "+"


def test_add_applied_accepts_non_numeric_ats_score(tracker_db) -> None:
    content = _build_content(
        "https://example.com/jobs/4",
        Path("/tmp") / "Applications" / "2026-04-16" / "Acme",
    )
    content["ats_score"] = "N/A"

    assert tracker.add_applied(content, force=False) is True

    rows = lookup_url("https://example.com/jobs/4")
    assert rows[0]["ats"] == "N/A"


def test_add_applied_removes_manual_pending_row_first(tracker_db) -> None:
    url = "https://www.jobleads.com/pl/job/x--poland--aaa111deadbeef0000000000000000"
    assert (
        tracker.add_manual_jobleads_pending(
            url=url,
            company="GammaInc",
            title="Dev",
            folder_abs=Path("/tmp/folder"),
        )
        is True
    )

    content = _build_content(url, Path("/tmp/folder"))
    assert tracker.add_applied(content, force=False) is True

    rows = lookup_url(url)
    # MANUAL row must be gone, only the applied row remains
    assert len(rows) == 1
    assert rows[0]["ats"] == "85%"


def test_add_applied_converts_10_point_scale_to_percent(tracker_db) -> None:
    content = _build_content(
        "https://example.com/jobs/5",
        Path("/tmp") / "Applications" / "2026-04-16" / "Acme",
    )
    content["ats_score"] = "8/10"

    assert tracker.add_applied(content, force=False) is True

    rows = lookup_url("https://example.com/jobs/5")
    assert rows[0]["ats"] == "80%"


def test_apply_pull_updates_updates_fields(tracker_db) -> None:
    """apply_pull_updates writes Sent, Re-application, To Learn by ID."""
    content = {
        "company_name": "PullCo",
        "job_title": "Frontend Dev",
        "stack": "Angular",
        "ats_score": "90",
        "apply_url": "https://example.com/pull/1",
        "output_folder": "/tmp/PullCo",
        "to_learn": "",
    }
    assert add_applied(content)

    rows = lookup_url("https://example.com/pull/1")
    assert rows
    row_id = rows[0]["id"]

    count = apply_pull_updates(
        [
            {
                "ID": row_id,
                "Sent": "2026-05-14",
                "Re-application": "+",
                "To Learn": "RxJS",
            }
        ]
    )
    assert count == 1

    from hunter.db import get_db

    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT sent, reapplication, to_learn FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    assert row["sent"] == "2026-05-14"
    assert row["reapplication"] == "+"
    assert row["to_learn"] == "RxJS"


def test_apply_pull_updates_skips_row_that_turned_dirty_after_merge_read(tracker_db) -> None:
    """AND sheets_dirty=0 race guard: a row marked dirty after the pull's merge

    read (e.g. a concurrent web-UI edit) must not be clobbered by the stale
    Sheets value the merge already decided to write.
    """
    content = {
        "company_name": "RaceCo",
        "job_title": "Backend Dev",
        "stack": "Python",
        "ats_score": "60",
        "apply_url": "https://example.com/race/1",
        "output_folder": "/tmp/RaceCo",
        "to_learn": "",
    }
    assert add_applied(content)

    rows = lookup_url("https://example.com/race/1")
    row_id = rows[0]["id"]

    # Simulate a web-UI write landing between _apply_pull_delta_db's merge read
    # and apply_pull_updates' write.
    tracker.mark_sheets_dirty(row_id)

    count = apply_pull_updates(
        [
            {
                "ID": row_id,
                "Sent": "2026-05-14",
                "Re-application": "",
                "To Learn": "",
            }
        ]
    )
    assert count == 0

    from hunter.db import get_db

    with get_db(tracker_db) as conn:
        row = conn.execute(
            "SELECT sent, sheets_dirty FROM applications WHERE id=?", (row_id,)
        ).fetchone()
    # Sent stays whatever add_applied wrote (blank) — the pull's Sheets value
    # never landed — and sheets_dirty is untouched (still 1).
    assert row["sent"] != "2026-05-14"
    assert row["sheets_dirty"] == 1


def test_apply_pull_updates_noop_for_unknown_id(tracker_db) -> None:
    content = {
        "company_name": "Ghost",
        "job_title": "Dev",
        "stack": "React",
        "ats_score": "70",
        "apply_url": "https://example.com/ghost/1",
        "output_folder": "/tmp/Ghost",
        "to_learn": "",
    }
    assert add_applied(content)

    count = apply_pull_updates(
        [
            {
                "ID": "nonexistent",
                "Sent": "2026-05-14",
                "Re-application": "",
                "To Learn": "",
            }
        ]
    )
    assert count == 0
