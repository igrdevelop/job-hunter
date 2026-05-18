"""B3 — tracker must persist normalized URLs so dedup is param-order-independent."""
import datetime
from pathlib import Path
from unittest.mock import patch

import openpyxl
import pytest

from hunter.tracker import (
    add_applied,
    add_skipped,
    add_failed,
    add_expired,
    get_known_urls,
    normalize_url,
    URL_COL_INDEX,
)
from hunter.models import Job


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_content(url: str, company: str = "Acme", title: str = "Angular Dev") -> dict:
    return {
        "company_name": company,
        "job_title": title,
        "stack": "Angular",
        "apply_url": url,
        "output_folder": "Applications/2099-01-01/Acme",
        "ats_score": "95",
        "cover_letter": "Dear...",
        "to_learn": "",
    }


def _read_url_col(path: Path) -> list[str]:
    """Read the raw URL column values from tracker (no normalization applied)."""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    results = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row and len(row) >= URL_COL_INDEX:
            val = row[URL_COL_INDEX - 1]
            results.append(str(val or ""))
    wb.close()
    return results


def _make_job(url: str, company: str = "Acme", title: str = "Angular Dev") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None,
               url=url, source="test")


# ---------------------------------------------------------------------------
# add_applied writes normalized URL
# ---------------------------------------------------------------------------

def test_add_applied_writes_normalized_url(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker.xlsx"
    raw_url = (
        "https://www.pracuj.pl/praca/angular-dev,oferta,1004756554"
        "?sendid=abc123&utm_source=newsletter&sug=xyz"
    )
    expected = normalize_url(raw_url)

    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_applied(_make_content(raw_url))

    stored = _read_url_col(tracker)
    assert stored, "no row written"
    assert stored[0] == expected, f"stored {stored[0]!r} != {expected!r}"


def test_add_applied_unknown_param_still_normalized(tmp_path: Path) -> None:
    """Params not in the drop-list must be stripped when written to tracker."""
    tracker = tmp_path / "tracker.xlsx"
    # 'jobAlertId' is NOT in the current drop-list — it must be stripped on write
    raw_url = "https://www.pracuj.pl/praca/angular-dev,oferta,99?jobAlertId=42"
    expected = "https://www.pracuj.pl/praca/angular-dev,oferta,99"

    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_applied(_make_content(raw_url))

    stored = _read_url_col(tracker)
    assert stored[0] == expected, f"stored {stored[0]!r} != {expected!r}"


def test_add_applied_url_dedup_catches_different_params(tmp_path: Path) -> None:
    """Same job scraped twice with different tracking params must not produce two rows."""
    tracker = tmp_path / "tracker.xlsx"
    url_v1 = "https://justjoin.it/job-offer/acme-angular-dev?utm_source=email"
    url_v2 = "https://justjoin.it/job-offer/acme-angular-dev?utm_source=push"

    with patch("hunter.tracker.TRACKER_PATH", tracker):
        written1 = add_applied(_make_content(url_v1))
        written2 = add_applied(_make_content(url_v2))

    assert written1 is True
    assert written2 is False, "second apply with same base URL should be rejected"


# ---------------------------------------------------------------------------
# add_skipped writes normalized URL
# ---------------------------------------------------------------------------

def test_add_skipped_writes_normalized_url(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker.xlsx"
    raw_url = "https://nofluffjobs.com/pl/job/acme-angular-dev?ref=homepage&utm_campaign=X"
    expected = normalize_url(raw_url)

    job = _make_job(raw_url)
    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_skipped(job)

    stored = _read_url_col(tracker)
    assert stored, "no row written"
    assert stored[0] == expected


# ---------------------------------------------------------------------------
# add_failed writes normalized URL
# ---------------------------------------------------------------------------

def test_add_failed_writes_normalized_url(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker.xlsx"
    raw_url = "https://justjoin.it/job-offer/acme-dev?trackingId=T99&utm_source=jj"
    expected = normalize_url(raw_url)

    job = _make_job(raw_url)
    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_failed(job)

    stored = _read_url_col(tracker)
    assert stored[0] == expected


# ---------------------------------------------------------------------------
# add_expired writes normalized URL
# ---------------------------------------------------------------------------

def test_add_expired_writes_normalized_url(tmp_path: Path) -> None:
    tracker = tmp_path / "tracker.xlsx"
    raw_url = "https://www.pracuj.pl/praca/dev,oferta,5555?sendid=XXX"
    expected = normalize_url(raw_url)

    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_expired(raw_url, company="Acme", title="Angular Dev")

    stored = _read_url_col(tracker)
    assert stored[0] == expected


# ---------------------------------------------------------------------------
# get_known_urls — round-trip sanity
# ---------------------------------------------------------------------------

def test_get_known_urls_matches_normalized_lookup(tmp_path: Path) -> None:
    """After write, get_known_urls must return the same key used by the hunt loop."""
    tracker = tmp_path / "tracker.xlsx"
    raw_url = "https://www.pracuj.pl/praca/test,oferta,1234?utm_source=alert&sendid=9"

    with patch("hunter.tracker.TRACKER_PATH", tracker):
        add_applied(_make_content(raw_url))
        known = get_known_urls()

    # The hunt loop does: normalize_url(j.url) in known_urls
    assert normalize_url(raw_url) in known
    # Must also match a re-scrape with different params
    rescrape_url = "https://www.pracuj.pl/praca/test,oferta,1234?utm_source=push&sendid=99"
    assert normalize_url(rescrape_url) in known
