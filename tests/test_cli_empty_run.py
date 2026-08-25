"""What happens when the CLI skill produces nothing usable.

docs/STACK_PRESCREEN_PLAN.md M6, revised after review.

`claude -p` is non-interactive, so a clarifying question does not pause the run,
it ends it. The fix is the prompt (.claude/commands/apply.md now opens with a
"decide, never ask" rule and no longer tells the skill to ask anywhere). This
module guards the code around it:

  * a run that created no folder stays a plain failure -- bounded by the FAIL
    row, the fail_count escalation and the consecutive-fail breaker. An earlier
    cut gave it a bespoke "retryable" outcome with no retry budget at all, which
    a review showed would let one chatty posting re-run every 30 s forever and
    freeze the whole apply queue behind it.
  * a run that created a folder but no documents is NOT the same thing:
    generate_docs writes the tracker row before the PDF step, so an APPLIED row
    can already exist. It is settled through the normal post-generation abort,
    or the Sheets and Drive backfills deliver a folder holding nothing but
    job_posting.txt.
  * a posting too short to generate from aborts before `claude -p` is spawned.
    main_cli had no floor at all, and with no job text the expired check, doomed
    gate, re-post gate and ATS verdict are all skipped -- the one run that most
    needs guarding ran with none of them.
"""

from datetime import date

import pytest

from hunter import tracker
from hunter.apply_shared import ApplyError
from hunter.config import PROJECT_DIR

POSTING = (
    "Job Title: Senior Angular Developer\nCompany: Nordic Labs\n"
    "Location: Fully remote (Poland)\n\n"
    "--- Job Description ---\n"
    "We are looking for a senior Angular engineer to own our design system. "
    "You will work with Angular, TypeScript and RxJS across several products, "
    "mentor other engineers and shape the frontend architecture. Fully remote "
    "within Poland, contract of employment or B2B, yearly training budget."
)


def _env(monkeypatch, tmp_path, *, posting=POSTING, folder_with=None, row_url=""):
    """Wire main_cli's boundaries.

    `folder_with` names the files the fake skill leaves behind; None means it
    creates no folder at all. `row_url`, when set, makes it also write the
    tracker row under that URL -- which is what the real skill does: it runs
    generate_docs without --no-tracker, and generate_docs writes the row BEFORE
    the PDF step.
    """
    applications = tmp_path / "Applications"
    applications.mkdir()
    monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", applications)
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", applications)
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda _u, **_kw: posting, raising=False)

    spawned = []

    class _Result:
        returncode = 0
        stdout = "Should I skip this one?"
        stderr = ""

    def _fake_run(cmd, **_kw):
        spawned.append(list(cmd))
        if folder_with is not None:
            folder = applications / date.today().strftime("%Y-%m-%d") / "NordicLabs"
            folder.mkdir(parents=True, exist_ok=True)
            for name, body in folder_with.items():
                (folder / name).write_text(body, encoding="utf-8")
            if row_url:
                tracker.add_applied(
                    {
                        "company_name": "Nordic Labs",
                        "job_title": "Senior Angular Developer",
                        "apply_url": row_url,
                        "stack": "Angular",
                        "ats_score": "94",
                        "output_folder": str(folder),
                    }
                )
        return _Result()

    monkeypatch.setattr("hunter.apply_cli.subprocess.run", _fake_run)

    from hunter import apply_cli as _mod

    _real_find = _mod._find_new_folder
    monkeypatch.setattr(
        "hunter.apply_cli._find_new_folder",
        lambda before, timeout=0: _real_find(before, timeout=0),
    )
    return applications, spawned


class TestNoFolderIsAPlainFailure:
    def test_it_raises_apply_error(self, tracker_db, tmp_path, monkeypatch):
        # "fail" is bounded: FAIL row, fail_count escalation, breaker. A bespoke
        # retryable outcome would have none of those.
        _env(monkeypatch, tmp_path, folder_with=None)
        from hunter.apply_cli import main_cli

        with pytest.raises(ApplyError):
            main_cli("https://example.com/jobs/1")


class TestFolderWithoutDocumentsIsSettled:
    URL = "https://example.com/jobs/2"

    def _run(self, tmp_path, monkeypatch):
        # The row must appear DURING the run, exactly as it does in production:
        # the skill runs generate_docs, which writes it before rendering the
        # PDFs. Writing it up front would trip main_cli's own dedup check first.
        applications, _ = _env(
            monkeypatch,
            tmp_path,
            folder_with={"content.json": "{}", "job_posting.txt": "posting"},
            row_url=self.URL,
        )
        folder = applications / date.today().strftime("%Y-%m-%d") / "NordicLabs"
        from hunter.apply_cli import main_cli

        return main_cli(self.URL), folder

    def test_it_does_not_raise(self, tracker_db, tmp_path, monkeypatch):
        result, _ = self._run(tmp_path, monkeypatch)
        assert result is None

    def test_the_applied_row_is_settled(self, tracker_db, tmp_path, monkeypatch):
        self._run(tmp_path, monkeypatch)

        rows = tracker.lookup_url(self.URL)
        assert rows and rows[0]["ats"].strip().upper() == "SKIP"
        assert not tracker.has_successful_entry(self.URL), (
            "otherwise the Sheets and Drive backfills deliver an empty folder"
        )


class TestTooShortPostingNeverSpawnsTheSkill:
    def test_it_aborts_before_the_subprocess(self, tracker_db, tmp_path, monkeypatch):
        _applications, spawned = _env(
            monkeypatch, tmp_path, posting="Frontend dev wanted.", folder_with=None
        )
        from hunter.apply_cli import main_cli

        assert main_cli("https://example.com/jobs/3") is None
        assert spawned == [], (
            "with no posting text every downstream screen is skipped, so the "
            "skill must not be started at all"
        )

    def test_a_real_posting_still_runs(self, tracker_db, tmp_path, monkeypatch):
        _applications, spawned = _env(monkeypatch, tmp_path, folder_with=None)
        from hunter.apply_cli import main_cli

        with pytest.raises(ApplyError):
            main_cli("https://example.com/jobs/4")
        assert spawned, "the floor must not swallow an ordinary posting"


class TestThePromptDoesNotAsk:
    @staticmethod
    def _prompt() -> str:
        return (PROJECT_DIR / ".claude" / "commands" / "apply.md").read_text(encoding="utf-8")

    def test_the_rule_is_present_and_early(self):
        prompt = self._prompt()
        assert "decide, never ask" in prompt.lower()
        assert "generate the package, always" in prompt.lower()
        assert prompt.index("never ask") < prompt.index("## Step 1"), (
            "a rule the model reads after it has already started working is not a rule"
        )

    def test_no_step_still_tells_the_skill_to_ask(self):
        # The rule at the top contradicted Step 2, which used to say "ask the
        # user to paste the job text manually" -- and the top rule outranking it
        # is how a package gets written for a posting nobody could read.
        prompt = self._prompt().lower()
        assert "ask the user to paste" not in prompt

    def test_the_unreadable_posting_branch_stops(self):
        prompt = self._prompt()
        assert "could not read the posting" in prompt


class TestNoBespokeOutcomeSurvives:
    def test_the_outcome_literal_is_unchanged(self):
        # Guard against re-introducing an unbounded "retryable" outcome: every
        # value here must have either a retry budget or a terminal row.
        import inspect

        from hunter.services import apply_service

        src = inspect.getsource(apply_service)
        assert "cli_no_output" not in src

    def test_no_dead_audit_entries(self):
        from hunter.apply_failures_log import LOGGED_OUTCOMES

        assert "cli_no_output" not in LOGGED_OUTCOMES


class TestFolderWithoutContentJson:
    """The state that made the abort crash instead of settling anything.

    A folder can exist with no content.json at all: the skill mkdir'd and died,
    or the subprocess timed out after the folder appeared (main_cli handles that
    shape explicitly). `_cli_content` is bound inside `if content_json_path
    .exists()`, so the abort at the bottom of the function referenced an unbound
    name and raised UnboundLocalError — which escapes apply_agent.main's
    `except (ApplyError, SystemExit)` entirely: no Telegram message, no row
    settled, and the empty folder shipped by the backfills half an hour later.
    """

    URL = "https://example.com/jobs/no-content-json"

    def test_it_settles_instead_of_crashing(self, tracker_db, tmp_path, monkeypatch):
        _env(
            monkeypatch,
            tmp_path,
            folder_with={"job_posting.txt": "posting"},
            row_url=self.URL,
        )
        from hunter.apply_cli import main_cli

        # No UnboundLocalError, no ApplyError — a clean settled abort.
        assert main_cli(self.URL) is None

        rows = tracker.lookup_url(self.URL)
        assert rows and rows[0]["ats"].strip().upper() == "SKIP"

    def test_it_settles_even_with_no_row_to_convert(self, tracker_db, tmp_path, monkeypatch):
        _env(monkeypatch, tmp_path, folder_with={"job_posting.txt": "posting"})
        from hunter.apply_cli import main_cli

        assert main_cli(self.URL) is None
