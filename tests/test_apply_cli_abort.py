"""Post-generation abort (docs/STACK_PRESCREEN_PLAN.md M1).

The CLI pipeline's abort stages run AFTER the CLI skill has rendered the
documents and written the tracker row (`.claude/commands/apply.md` calls
generate_docs.py without --no-tracker). Deleting the PDFs is not enough: the
row stays APPLIED, exit 0 makes apply_worker._resolve_outcome see
has_successful_entry, and the package is delivered to Sheets/Drive anyway.

That is the 2026-08-24 Interia incident (React-only vacancy, a 96% row, a
Drive folder and Sheets row 1339) plus 5 more in the same two-week window.
"""

import asyncio
from unittest.mock import AsyncMock

from hunter import apply_worker, tracker
from hunter.apply_shared import abort_after_generation
from hunter.models import Job

DASH = "—"
URL = "https://www.linkedin.com/jobs/view/4456671190"


def _applied_row(url: str = URL, folder: str = "/tmp/x") -> None:
    tracker.add_applied(
        {
            "company_name": "Interia",
            "job_title": "Regular Frontend Developer/ka",
            "apply_url": url,
            "stack": "React",
            "ats_score": "96",
            "output_folder": folder,
        }
    )


def _row(url: str = URL) -> dict:
    from hunter.db import get_db

    with get_db(tracker.DB_PATH) as conn:
        r = conn.execute(
            "SELECT * FROM applications WHERE url_norm=?", (tracker.normalize_url(url),)
        ).fetchone()
    return dict(r) if r else {}


def _job() -> Job:
    return Job(
        title="Regular Frontend Developer/ka",
        company="Interia",
        location="Remote",
        salary=None,
        url=URL,
        source="linkedin",
    )


def _package(tmp_path):
    """A rendered application folder, as generate_docs leaves it."""
    folder = tmp_path / "Interia"
    folder.mkdir()
    (folder / "CV_EN.pdf").write_text("pdf", encoding="utf-8")
    (folder / "Cover_Letter_EN.pdf").write_text("pdf", encoding="utf-8")
    (folder / "CV_EN.docx").write_text("docx", encoding="utf-8")
    (folder / "content.json").write_text("{}", encoding="utf-8")
    (folder / "job_posting.txt").write_text("posting", encoding="utf-8")
    return folder


class TestConvertOwnAppliedRow:
    def test_converts_in_place_keeping_id_and_sheets_row(self, tracker_db):
        _applied_row()
        before = _row()
        assert before["ats_status"] == "96%"

        assert tracker.convert_own_applied_row(URL) is True

        after = _row()
        assert after["ats_status"] == "SKIP"
        assert after["sent"] == DASH
        # id and sheets_row must survive: an already-mirrored Sheet row has to
        # be CORRECTED in place, not duplicated by a fresh append.
        assert after["id"] == before["id"]
        assert after["sheets_row"] == before["sheets_row"]
        assert after["sheets_dirty"] == 1
        # folder is deliberately kept (job_posting.txt stays for diagnostics)
        assert after["folder"] == before["folder"]

    def test_applied_row_is_no_longer_a_successful_entry(self, tracker_db):
        _applied_row()
        assert tracker.has_successful_entry(URL) is True
        tracker.convert_own_applied_row(URL)
        assert tracker.has_successful_entry(URL) is False

    def test_skip_row_is_left_alone(self, tracker_db):
        tracker.add_skipped(_job())
        assert tracker.convert_own_applied_row(URL) is False

    def test_failed_row_is_left_alone(self, tracker_db):
        tracker.add_failed(_job())
        assert tracker.convert_own_applied_row(URL) is False
        assert _row()["ats_status"] == "FAIL"

    def test_queue_placeholder_is_left_alone(self, tracker_db):
        # PENDING/IN_PROGRESS belong to the apply queue's own bookkeeping
        # (_clear_own_placeholder owns those), not to this converter.
        tracker.add_pending(_job())
        assert tracker.convert_own_applied_row(URL) is False
        assert _row()["ats_status"] == tracker.PENDING_ATS

    def test_blank_url_is_a_no_op(self, tracker_db):
        assert tracker.convert_own_applied_row("") is False


class TestAbortAfterGeneration:
    def test_drops_rendered_docs_but_keeps_diagnostics(self, tracker_db, tmp_path):
        folder = _package(tmp_path)
        _applied_row(folder=str(folder))

        abort_after_generation(folder, URL, reason="react-only stack")

        assert list(folder.glob("*.pdf")) == []
        assert list(folder.glob("*.docx")) == []
        assert (folder / "content.json").exists()
        assert (folder / "job_posting.txt").exists()

    def test_converts_the_row_and_reports_it(self, tracker_db, tmp_path):
        folder = _package(tmp_path)
        _applied_row(folder=str(folder))

        assert abort_after_generation(folder, URL, reason="react-only stack") is True
        assert _row()["ats_status"] == "SKIP"

    def test_reports_false_when_there_is_no_row_to_convert(self, tracker_db, tmp_path):
        # The caller then falls back to its own terminal write
        # (add_react_skipped / add_skipped) -- the pre-helper behavior.
        folder = _package(tmp_path)
        assert abort_after_generation(folder, URL, reason="react-only stack") is False

    def test_survives_a_missing_folder(self, tracker_db):
        _applied_row()
        assert abort_after_generation(None, URL, reason="lang gate") is True

    def test_notifies_when_given_a_message(self, tracker_db, monkeypatch):
        sent = []
        monkeypatch.setattr("hunter.apply_shared.notify", lambda m: sent.append(m))
        _applied_row()

        abort_after_generation(None, URL, reason="r", telegram_text="skipped")

        assert sent == ["skipped"]

    def test_stays_silent_without_a_message(self, tracker_db, monkeypatch):
        sent = []
        monkeypatch.setattr("hunter.apply_shared.notify", lambda m: sent.append(m))
        _applied_row()

        abort_after_generation(None, URL, reason="r")

        assert sent == []


class TestParentDoesNotDeliverAfterAbort:
    """The actual regression: exit 0 plus a live applied row means delivery."""

    def _resolve(self, monkeypatch):
        sent = []
        monkeypatch.setattr(
            apply_worker, "send_text", AsyncMock(side_effect=lambda _c, t: sent.append(t))
        )
        deliver = AsyncMock()
        monkeypatch.setattr("hunter.delivery.deliver_apply_now", deliver)
        asyncio.run(apply_worker._resolve_outcome(None, 0, _job(), "ok"))
        return deliver, sent

    def test_delivers_without_the_abort(self, tracker_db, monkeypatch):
        # Guard on the guard: this is the pre-fix behavior, and it is exactly
        # what shipped the Interia package to Sheets and Drive.
        _applied_row()
        deliver, _ = self._resolve(monkeypatch)
        deliver.assert_awaited_once_with(URL)

    def test_does_not_deliver_after_the_abort(self, tracker_db, tmp_path, monkeypatch):
        folder = _package(tmp_path)
        _applied_row(folder=str(folder))

        abort_after_generation(folder, URL, reason="react-only stack")

        deliver, sent = self._resolve(monkeypatch)
        deliver.assert_not_awaited()
        # A converted row is a soft terminal (SKIP): the worker stays quiet
        # instead of announcing "Done" or clearing the placeholder.
        assert sent == []
