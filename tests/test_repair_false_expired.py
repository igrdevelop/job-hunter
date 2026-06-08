"""Tests for tools/repair_false_expired.py — decision logic for un-EXPIRE repair."""

import importlib.util
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "repair_false_expired", ROOT / "tools" / "repair_false_expired.py"
)
repair = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(repair)


def test_trust_ids_skips_verify() -> None:
    row = {"url": "https://x/1", "company": "C", "title": "T"}
    with patch.object(repair, "fetch_job_text") as m:
        live, reason = repair._is_live(row, trust=True)
    assert live is True
    assert "explicit" in reason
    m.assert_not_called()  # no network when trusting the operator's list


def test_no_url_treated_live() -> None:
    live, reason = repair._is_live({"url": "", "company": "C", "title": "T"}, trust=False)
    assert live is True
    assert "no URL" in reason


def test_url_still_expired_kept() -> None:
    row = {"url": "https://x/1"}
    with patch.object(repair, "fetch_job_text", return_value="x" * 500), \
         patch.object(repair, "is_job_expired", return_value=True):
        live, reason = repair._is_live(row, trust=False)
    assert live is False
    assert "verified" in reason


def test_url_live_cleared() -> None:
    row = {"url": "https://x/1"}
    with patch.object(repair, "fetch_job_text", return_value="x" * 500), \
         patch.object(repair, "is_job_expired", return_value=False):
        live, reason = repair._is_live(row, trust=False)
    assert live is True


def test_fetch_error_is_undetermined() -> None:
    row = {"url": "https://x/1"}
    with patch.object(repair, "fetch_job_text", side_effect=RuntimeError("boom")):
        live, reason = repair._is_live(row, trust=False)
    assert live is None
    assert "fetch error" in reason


def test_thin_fetch_is_undetermined() -> None:
    row = {"url": "https://x/1"}
    with patch.object(repair, "fetch_job_text", return_value="too short"):
        live, reason = repair._is_live(row, trust=False)
    assert live is None
    assert "thin fetch" in reason
