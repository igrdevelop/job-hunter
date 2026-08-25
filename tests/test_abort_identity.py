"""Second adversarial review of the post-generation abort (2026-08-24).

The reviewer reproduced two destructive defects against the real tracker, and
found a third flow the guards had quietly broken. Each one gets a test here.

1. The conversion matched a `<date>/<Company>` folder SUFFIX and updated every
   row that matched, so a second run for the same company on the same day under
   a different root converted a GENUINE, already-delivered application to SKIP.
2. That suffix went into a SQL LIKE with no ESCAPE, and `_` is a LIKE wildcard --
   and `_` is exactly what `_sanitize_folder_company` substitutes for every
   illegal character, what `compute_output_folder` appends (`_2`), and what the
   re-post gate writes (`{Company}_reused_{date}`).
3. The JobLeads MANUAL flow has ALWAYS written a folder on a MANUAL row, so
   putting MANUAL in the "not an application" set stopped its folder from ever
   reaching Drive -- while the bot's own message tells the owner to open that
   folder and paste the job text into it.
"""

import asyncio

import pytest

from hunter import delivery, tracker
from hunter.apply_shared import abort_after_generation


def _applied(url: str, folder: str, company: str = "Interia") -> None:
    tracker.add_applied(
        {
            "company_name": company,
            "job_title": "Frontend Developer",
            "apply_url": url,
            "stack": "React",
            "ats_score": "96",
            "output_folder": folder,
        }
    )


def _status(url: str) -> str:
    rows = tracker.lookup_url(url)
    return rows[0]["ats"].strip().upper() if rows else ""


class TestConversionNeverTouchesASecondRow:
    def test_two_roots_same_date_and_company_convert_nothing(self, tracker_db):
        # Reproduction from the review. The two roots are not invented: the
        # commit that introduced the suffix match cited the CLI skill resolving
        # output_folder against the wrong root (Ness Solution, 2026-08-21) as
        # the reason it existed.
        _applied("https://a.example/jobs/1", "/app/users/1/Applications/2026-08-24/Interia")
        _applied("https://b.example/jobs/2", "/app/Applications/2026-08-24/Interia")

        converted = tracker.convert_own_applied_row(
            "https://b.example/jobs/2", folder="/app/Applications/2026-08-24/Interia"
        )

        assert converted is True
        assert _status("https://a.example/jobs/1") == "96%", (
            "a genuine, already-delivered application must never be collateral"
        )
        assert _status("https://b.example/jobs/2") == "SKIP"

    def test_an_ambiguous_folder_converts_nothing_at_all(self, tracker_db):
        # Same folder string on two rows: there is no safe way to pick one, so
        # a destructive write must refuse rather than guess.
        _applied("https://a.example/jobs/1", "/app/Applications/2026-08-24/Interia")
        _applied("https://b.example/jobs/2", "/app/Applications/2026-08-24/Interia")

        assert (
            tracker.convert_own_applied_row("", folder="/app/Applications/2026-08-24/Interia")
            is False
        )
        assert _status("https://a.example/jobs/1") == "96%"
        assert _status("https://b.example/jobs/2") == "96%"

    def test_underscores_in_a_company_folder_are_not_wildcards(self, tracker_db):
        # `_sanitize_folder_company` turns every illegal character into `_`, so
        # underscores are ordinary here, not exotic.
        _applied("https://a.example/jobs/1", "/app/Applications/2026-08-24/AXB Testing")

        assert tracker.convert_own_applied_row("", folder="/other/2026-08-24/A_B Testing") is False
        assert _status("https://a.example/jobs/1") == "96%"


class TestIdentityComesFromTheContent:
    def test_paste_mode_row_is_found_by_its_own_output_folder(self, tracker_db, tmp_path):
        # Paste mode leaves url_norm='' on the row; the pipeline's `url` is
        # useless as a key, but content.json holds the exact folder string
        # add_applied stored.
        folder = tmp_path / "2026-08-24" / "Interia"
        folder.mkdir(parents=True)
        _applied("", str(folder))

        converted = abort_after_generation(
            folder,
            "",
            reason="react-only stack",
            content={"output_folder": str(folder), "company_name": "Interia"},
        )

        assert converted is True

    def test_apply_button_url_divergence_is_survivable(self, tracker_db, tmp_path):
        # .claude/commands/apply.md lets the skill record the apply-button URL
        # instead of the input one, so the row can sit under a URL the pipeline
        # never sees.
        folder = tmp_path / "Interia"
        folder.mkdir()
        _applied("https://ats.example/apply/9", str(folder))

        converted = abort_after_generation(
            folder,
            "https://www.linkedin.com/jobs/view/1",
            reason="react-only stack",
            content={"apply_url": "https://ats.example/apply/9", "output_folder": str(folder)},
        )

        assert converted is True
        assert _status("https://ats.example/apply/9") == "SKIP"


class TestSettlingNothingIsLoud:
    def test_it_raises_so_best_effort_can_count_it(self, tracker_db, tmp_path, monkeypatch):
        # The failure mode with no exception of its own: convert finds nothing
        # AND add_skipped no-ops because an unrelated terminal row matches. Two
        # of the four call sites cannot see the return value, so silence here
        # would restore the original incident invisibly.
        seen = []
        monkeypatch.setattr("hunter.apply_shared.notify", lambda m: seen.append(m))
        monkeypatch.setattr("hunter.tracker.add_skipped", lambda _job: None)

        alerted = []
        import hunter.best_effort as be

        monkeypatch.setattr(be, "_record_failure", lambda *a, **kw: alerted.append(a))

        folder = tmp_path / "Interia"
        folder.mkdir()

        result = abort_after_generation(folder, "https://x.example/1", reason="r", content={})

        assert result is False
        assert alerted, "best_effort must see a failure it can alert on"

    def test_a_written_skip_row_is_not_a_failure(self, tracker_db, tmp_path, monkeypatch):
        alerted = []
        import hunter.best_effort as be

        monkeypatch.setattr(be, "_record_failure", lambda *a, **kw: alerted.append(a))

        folder = tmp_path / "Interia"
        folder.mkdir()

        abort_after_generation(
            folder,
            "https://x.example/1",
            reason="r",
            content={"company_name": "Acme", "job_title": "FE"},
        )

        assert _status("https://x.example/1") == "SKIP"
        assert alerted == []


class TestJobLeadsManualFlowStillDelivers:
    URL = "https://www.jobleads.com/job/abc"

    def _manual_row(self, tmp_path):
        folder = tmp_path / "2026-08-24" / "Acme"
        folder.mkdir(parents=True)
        tracker.add_manual_jobleads_pending(
            url=self.URL, company="Acme", title="Frontend Developer", folder_abs=folder
        )
        return folder

    def test_delivery_is_not_blocked(self, tracker_db, tmp_path, monkeypatch):
        # apply_service returns outcome "manual"; bot.apply_runner falls through
        # to deliver_apply_now, and the owner is told to open the Drive folder
        # and paste the job text into it.
        self._manual_row(tmp_path)
        calls = []

        async def _mirror(_url):
            calls.append("mirror")
            return True

        async def _upload(_url):
            calls.append("upload")
            return "https://drive.example/x"

        monkeypatch.setattr(delivery, "_mirror_row_targeted", _mirror)
        monkeypatch.setattr(delivery, "_upload_folder_targeted", _upload)
        monkeypatch.setattr(delivery, "_push_missing_rows", _noop())
        monkeypatch.setattr(delivery, "_upload_missing_folders", _noop())

        asyncio.run(delivery.deliver_apply_now(self.URL))

        assert calls == ["mirror", "upload"]

    def test_the_drive_backfill_still_picks_up_its_folder(self, tracker_db, tmp_path, monkeypatch):
        folder = self._manual_row(tmp_path)
        from hunter import gdrive_sync

        monkeypatch.setattr(gdrive_sync, "_ready", lambda: True)
        monkeypatch.setattr(
            "hunter.tracker.read_all_tracker_rows",
            lambda: [
                {
                    "Folder": str(folder),
                    "ATS %": "MANUAL",
                    "Drive URL": "",
                    "Company": "Acme",
                    "URL": self.URL,
                }
            ],
        )

        async def _no_shadows(_folders, force=False):
            return 0, 0, []

        monkeypatch.setattr(gdrive_sync, "_upload_shadow_subfolders", _no_shadows)
        reached = []

        async def _reached(_fn):
            reached.append(True)
            raise RuntimeError("root resolution stub")

        monkeypatch.setattr(gdrive_sync, "_call_with_reauth", _reached)

        asyncio.run(gdrive_sync.upload_missing_folders(tmp_path))

        assert reached == [True], "a MANUAL row has always carried a real folder"


def _noop():
    async def _f(*_a, **_kw):
        return None

    return _f


class TestManualFlagIsAnExplicitOptIn:
    @staticmethod
    def _capture(monkeypatch):
        captured = []

        class _P:
            returncode = 0

            async def communicate(self):
                return b"", b""

        async def _fake_exec(*args, **_kw):
            captured.append(list(args))
            return _P()

        monkeypatch.setattr(
            "hunter.services.apply_service.asyncio.create_subprocess_exec", _fake_exec
        )
        return captured

    @pytest.mark.parametrize("is_manual,expected", [(True, True), (False, False)])
    def test_flag_follows_the_argument_not_the_function(self, monkeypatch, is_manual, expected):
        # "reached the manual runner" and "the owner saw this vacancy" are not
        # the same claim: a bulk expansion of one pasted link into dozens of job
        # ids must not inherit the flag just by sharing a code path.
        from pathlib import Path

        from hunter.services.apply_service import run_apply_agent_for_url

        captured = self._capture(monkeypatch)
        asyncio.run(
            run_apply_agent_for_url(
                url="https://example.com/jobs/1",
                timeout_sec=5,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
                is_manual=is_manual,
            )
        )

        assert ("--manual" in captured[0]) is expected

    def test_the_telegram_runner_opts_in(self):
        import inspect

        from hunter.bot import apply_runner

        src = inspect.getsource(apply_runner._run_apply_agent)
        assert "is_manual=True" in src


class TestDedupExclusionTakesExactlyOneRow:
    """Excluding by folder must not hide a genuine duplicate.

    `.claude/commands/apply.md` tells the skill to name the folder
    "{date}/{CompanyName}" with no `_2` collision suffix — that logic lives in
    compute_output_folder, which only the API path uses. So two CLI applications
    for the same company on the same day carry an IDENTICAL folder string, and
    dropping every row that matches it would hide the earlier one from the
    company+title gate. That is exactly the Comarch case the gate was added for
    on 2026-08-20: one requisition arriving via LinkedIn and via pracuj.pl in the
    same day's hunts.
    """

    FOLDER = "/app/Applications/2026-08-25/Comarch"

    def _two_rows_one_folder(self):
        tracker.add_applied(
            {
                "company_name": "Comarch",
                "job_title": "Senior Angular Developer",
                "apply_url": "https://linkedin.com/jobs/view/1",
                "stack": "Angular",
                "ats_score": "94",
                "output_folder": self.FOLDER,
            }
        )
        tracker.add_applied(
            {
                "company_name": "Comarch",
                "job_title": "Senior Angular Developer",
                "apply_url": "https://pracuj.pl/oferta/2",
                "stack": "Angular",
                "ats_score": "96",
                "output_folder": self.FOLDER,
            }
        )

    def test_the_earlier_duplicate_stays_visible(self, tracker_db):
        self._two_rows_one_folder()
        key = tracker.dedup_key("Comarch", "Senior Angular Developer")

        known = tracker.get_known_company_titles(
            exclude_url="https://pracuj.pl/oferta/2", exclude_folder=self.FOLDER
        )

        assert key in known, (
            "the run's own row is excluded, but the earlier application sharing "
            "its folder must still be seen — otherwise the gate lets a real "
            "duplicate through"
        )

    def test_the_runs_own_row_is_still_excluded(self, tracker_db):
        # Single row: the gate must not match itself (the bug the golden CLI
        # suite found on its first run).
        tracker.add_applied(
            {
                "company_name": "Solo Corp",
                "job_title": "Frontend Developer",
                "apply_url": "https://example.com/only",
                "stack": "Angular",
                "ats_score": "95",
                "output_folder": "/app/Applications/2026-08-25/SoloCorp",
            }
        )
        key = tracker.dedup_key("Solo Corp", "Frontend Developer")

        known = tracker.get_known_company_titles(
            exclude_url="https://example.com/only",
            exclude_folder="/app/Applications/2026-08-25/SoloCorp",
        )

        assert key not in known

    def test_no_exclusion_sees_everything(self, tracker_db):
        self._two_rows_one_folder()
        assert tracker.dedup_key("Comarch", "Senior Angular Developer") in (
            tracker.get_known_company_titles()
        )
