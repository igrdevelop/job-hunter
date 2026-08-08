"""SKIP/FAIL rows carry Sent='—' so they never appear in the Sheets
"Sent is empty" send-queue filter view (owner request 2026-08-08).

Covers:
- add_skipped / add_failed stamp the dash at insert (DB + returned mirror dict)
- the init_db data backfill stamps pre-existing blank-Sent SKIP/FAIL rows
  (and marks them sheets_dirty so the resync pushes the dash to the Sheet)
- the FAIL retry loop still picks dash-marked rows up (retries key on
  ats_status alone — the dash must not shadow-disable retries)
"""

from hunter.db import get_db, init_db
from hunter.models import Job
from hunter.tracker import (
    add_failed,
    add_skipped,
    get_failed_jobs,
    lookup_url,
)


def _make_job(url: str, company: str = "Acme", title: str = "Angular Dev") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source="test")


def test_add_skipped_stamps_sent_dash(tracker_db) -> None:
    url = "https://justjoin.it/job-offer/acme-angular-dev"
    row = add_skipped(_make_job(url))

    assert row is not None
    assert row["Sent"] == "—", "mirror dict must carry the dash for the Sheets append"

    db_rows = lookup_url(url)
    assert db_rows and db_rows[0]["sent"] == "—"


def test_add_failed_stamps_sent_dash(tracker_db) -> None:
    url = "https://justjoin.it/job-offer/acme-dev-fail"
    add_failed(_make_job(url))

    db_rows = lookup_url(url)
    assert db_rows and db_rows[0]["sent"] == "—"


def test_failed_row_with_dash_is_still_retried(tracker_db) -> None:
    """The retry loop selects by ats_status='FAIL' only — the dash must not hide the row."""
    url = "https://justjoin.it/job-offer/acme-dev-retry"
    add_failed(_make_job(url))

    retry_urls = [j.url for j in get_failed_jobs()]
    assert any("acme-dev-retry" in u for u in retry_urls)


def test_init_db_backfills_blank_sent_on_skip_and_fail_rows(tracker_db) -> None:
    """Pre-2026-08-08 SKIP/FAIL rows (blank Sent) get the dash + sheets_dirty on init."""
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, sent)"
            " VALUES ('old1skip', '2026-07-20', 'Old', 'Dev', 'SKIP', 'https://x.com/a',"
            " 'https://x.com/a', '')"
        )
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, sent)"
            " VALUES ('old2fail', '2026-07-20', 'Old', 'Dev', 'FAIL', 'https://x.com/b',"
            " 'https://x.com/b', '')"
        )
        # An applied row with a blank Sent is genuinely awaiting send — untouched.
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, sent)"
            " VALUES ('applied1', '2026-07-20', 'Old', 'Dev', '92%', 'https://x.com/c',"
            " 'https://x.com/c', '')"
        )

    init_db(tracker_db, xlsx_path=tracker_db.parent / "no_tracker.xlsx")

    with get_db(tracker_db) as conn:
        rows = {
            r["id"]: (r["sent"], r["sheets_dirty"])
            for r in conn.execute("SELECT id, sent, sheets_dirty FROM applications")
        }
    assert rows["old1skip"] == ("—", 1)
    assert rows["old2fail"] == ("—", 1)
    assert rows["applied1"] == ("", 0), "applied rows awaiting send must stay blank"


def test_backfill_never_overwrites_existing_sent(tracker_db) -> None:
    """A SKIP row already annotated (EXPIRED, a date, a note) keeps its value."""
    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, sent)"
            " VALUES ('expired1', '2026-07-20', 'Old', 'Dev', 'SKIP', 'https://x.com/d',"
            " 'https://x.com/d', 'EXPIRED')"
        )

    init_db(tracker_db, xlsx_path=tracker_db.parent / "no_tracker.xlsx")

    with get_db(tracker_db) as conn:
        row = conn.execute("SELECT sent FROM applications WHERE id='expired1'").fetchone()
    assert row["sent"] == "EXPIRED"
