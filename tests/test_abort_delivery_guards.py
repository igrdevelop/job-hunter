"""Guards found by the adversarial review of M1 (docs/STACK_PRESCREEN_PLAN.md).

M1 converted the applied row on a post-generation abort and relied on
`apply_worker._resolve_outcome` refusing to deliver a non-successful row. The
review showed that reasoning covered one of four delivery parents, and that the
converted row -- the first SKIP row in this codebase that KEEPS its folder --
walks straight into the Drive backfill. These are the regression tests for both
holes, plus the folder-based identity that makes the conversion actually fire on
the paste flow.
"""

import asyncio

import pytest

from hunter import delivery, tracker
from hunter.apply_shared import abort_after_generation

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


def _status(url: str = URL) -> str:
    rows = tracker.lookup_url(url)
    return rows[0]["ats"].strip().upper() if rows else ""


class TestFolderIsAlsoAnIdentity:
    def test_converts_a_paste_mode_row_that_has_no_url(self, tracker_db, tmp_path):
        # Paste mode never hands the CLI skill a URL, so add_applied writes
        # url_norm=''. Keying the conversion on the URL alone made the whole
        # paste flow -- including every linkedin_scout_relay post -- a no-op.
        folder = tmp_path / "2026-08-24" / "Interia"
        folder.mkdir(parents=True)
        _applied_row(url="", folder=str(folder))

        assert tracker.convert_own_applied_row("", folder=str(folder)) is True

    def test_a_differently_spelled_folder_is_refused_not_guessed(self, tracker_db, tmp_path):
        # The CLI skill has resolved output_folder against the wrong root before
        # (Ness Solution, 2026-08-21). An earlier cut tried to paper over that
        # with a "<date>/<Company>" suffix match, and a review reproduced it
        # converting a SECOND, genuine application. The answer is not a cleverer
        # guess: the caller passes the row's OWN output_folder from content.json
        # (see tests/test_abort_identity.py), and a mismatch converts nothing.
        _applied_row(folder="Applications/2026-08-24/Interia")
        absolute = tmp_path / "app" / "Applications" / "2026-08-24" / "Interia"

        assert tracker.convert_own_applied_row(url="", folder=str(absolute)) is False
        assert _status() == "96%"

    def test_url_alone_still_works(self, tracker_db):
        _applied_row()
        assert tracker.convert_own_applied_row(URL) is True

    def test_neither_key_is_a_no_op(self, tracker_db):
        _applied_row()
        assert tracker.convert_own_applied_row("", folder="") is False
        assert _status() == "96%"


class TestAbortAlwaysLeavesATerminalRow:
    def test_writes_a_skip_row_when_there_was_nothing_to_convert(self, tracker_db, tmp_path):
        # Without this, the run ends with no terminal row: the worker clears the
        # placeholder, the vacancy returns next hunt, the CLI regenerates the
        # whole package and the same gate blocks again -- forever. Exactly the
        # defect fixed for the backend-only skip on 2026-08-17.
        folder = tmp_path / "Interia"
        folder.mkdir()

        abort_after_generation(
            folder,
            URL,
            reason="claim judge blocked delivery",
            content={"company_name": "Interia", "job_title": "Dev"},
        )

        assert _status() == "SKIP", "an abort must always leave something terminal on record"

    def test_reports_conversion_separately_from_the_fallback(self, tracker_db, tmp_path):
        folder = tmp_path / "Interia"
        folder.mkdir()
        _applied_row(folder=str(folder))

        assert (
            abort_after_generation(
                folder, URL, reason="r", content={"apply_url": URL, "output_folder": str(folder)}
            )
            is True
        )


class TestDeliveryRefusesANonAppliedRow:
    @staticmethod
    def _spy(monkeypatch) -> dict:
        seen = {"mirror": 0, "push": 0, "upload": 0, "backfill": 0}

        async def _mirror(_url):
            seen["mirror"] += 1
            return True

        async def _push():
            seen["push"] += 1

        async def _upload(_url):
            seen["upload"] += 1
            return "https://drive.example/x"

        async def _backfill():
            seen["backfill"] += 1

        monkeypatch.setattr(delivery, "_mirror_row_targeted", _mirror)
        monkeypatch.setattr(delivery, "_push_missing_rows", _push)
        monkeypatch.setattr(delivery, "_upload_folder_targeted", _upload)
        monkeypatch.setattr(delivery, "_upload_missing_folders", _backfill)
        return seen

    def test_a_converted_row_is_not_delivered_anywhere(self, tracker_db, monkeypatch):
        # The manual paste / Apply-button parent (bot.apply_runner) delivers on
        # a plain exit 0 with no tracker check, so the gate has to live here.
        _applied_row()
        tracker.convert_own_applied_row(URL)
        seen = self._spy(monkeypatch)

        assert asyncio.run(delivery.deliver_apply_now(URL)) is None
        assert seen == {"mirror": 0, "push": 0, "upload": 0, "backfill": 0}, (
            "an aborted run must not reach Sheets, Drive, or the backfills"
        )

    def test_a_real_application_still_delivers(self, tracker_db, monkeypatch):
        _applied_row()
        seen = self._spy(monkeypatch)

        assert asyncio.run(delivery.deliver_apply_now(URL)) == "https://drive.example/x"
        assert seen["mirror"] == 1 and seen["upload"] == 1

    def test_an_unknown_url_still_falls_through_to_the_backfills(self, tracker_db, monkeypatch):
        # The CLI pipeline does not reliably own the URL its row was written
        # under, so "no row for this URL" must keep behaving as before rather
        # than silently dropping a real application.
        seen = self._spy(monkeypatch)
        monkeypatch.setattr(delivery, "_mirror_row_targeted", _missing_mirror(seen))
        monkeypatch.setattr(delivery, "_upload_folder_targeted", _missing_upload(seen))

        asyncio.run(delivery.deliver_apply_now("https://example.com/never-tracked"))

        assert seen["push"] == 1 and seen["backfill"] == 1


def _missing_mirror(seen):
    async def _mirror(_url):
        seen["mirror"] += 1
        return False

    return _mirror


def _missing_upload(seen):
    async def _upload(_url):
        seen["upload"] += 1
        return None

    return _upload


class TestDriveBackfillSkipsNonApplications:
    """A folder on a SKIP row is not an application.

    Until the M1 conversion existed, every SKIP producer wrote folder='', so
    "has a folder" was a safe proxy for "was generated" and the backfill never
    needed to look at the status.
    """

    @staticmethod
    def _run(monkeypatch, rows, tmp_path):
        from hunter import gdrive_sync

        for row in rows:
            (tmp_path / row["Folder"]).mkdir(parents=True, exist_ok=True)
            row["Folder"] = str(tmp_path / row["Folder"])

        monkeypatch.setattr(gdrive_sync, "_ready", lambda: True)
        monkeypatch.setattr("hunter.tracker.read_all_tracker_rows", lambda: rows)

        async def _no_shadows(_folders, force=False):
            return 0, 0, []

        monkeypatch.setattr(gdrive_sync, "_upload_shadow_subfolders", _no_shadows)

        reached_upload = []

        async def _reached(_fn):
            reached_upload.append(True)
            raise AssertionError("should not be reached in these tests")

        monkeypatch.setattr(gdrive_sync, "_call_with_reauth", _reached)
        return gdrive_sync, reached_upload

    def _row(self, status: str, folder: str) -> dict:
        return {
            "Folder": folder,
            "ATS %": status,
            "Drive URL": "",
            "Company": "Interia",
            "URL": URL,
        }

    def test_a_skip_row_with_a_folder_is_not_uploaded(self, monkeypatch, tmp_path):
        gdrive_sync, reached = self._run(
            monkeypatch, [self._row("SKIP", "2026-08-24/Interia")], tmp_path
        )

        result = asyncio.run(gdrive_sync.upload_missing_folders(tmp_path))

        assert result["uploaded"] == 0
        assert reached == [], "the backfill must not even resolve the Drive root for a SKIP row"

    # MANUAL is deliberately absent: add_manual_jobleads_pending has always
    # written a folder and the owner is told to open it on Drive.
    @pytest.mark.parametrize("status", ["FAIL", "EXPIRED"])
    def test_other_non_applications_are_skipped_too(self, monkeypatch, tmp_path, status):
        gdrive_sync, reached = self._run(
            monkeypatch, [self._row(status, "2026-08-24/Interia")], tmp_path
        )

        assert asyncio.run(gdrive_sync.upload_missing_folders(tmp_path))["uploaded"] == 0
        assert reached == []

    def test_a_real_application_still_reaches_the_uploader(self, monkeypatch, tmp_path):
        gdrive_sync, reached = self._run(
            monkeypatch, [self._row("96%", "2026-08-24/Interia")], tmp_path
        )

        # _call_with_reauth raises by design here (best_effort swallows it):
        # getting that far is the proof that the row was selected for upload.
        result = asyncio.run(gdrive_sync.upload_missing_folders(tmp_path))

        assert reached == [True]
        assert result["errors"], "the run reached root resolution and reported its failure"
