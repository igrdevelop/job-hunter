"""tests/test_schedules_profile_jobs.py — hunter/schedules/profile_jobs.py's
drain loop: render/parse/preview job execution, path-traversal safety, and
the claim -> process -> finish/fail lifecycle end-to-end.

docs/RESUME_PROFILE_STORE_PLAN.md step 4b.
docs/PROFILE_PAGE_TABS_WORKORDER.md: the 'preview' kind (bot-repo work item).
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from hunter import profile_jobs, profile_preview
from hunter.db import get_db
from hunter.schedules import profile_jobs as sched
from hunter.schedules.profile_jobs import (
    _resolve_user_relative_path,
    _run_preview_job,
    _run_render_job,
    drain_once,
)

EXAMPLE_PROFILE_PATH = Path(__file__).resolve().parent.parent / "candidate" / "profile.example.json"


@pytest.fixture()
def jobs_db(tracker_db, monkeypatch):
    monkeypatch.setattr(profile_jobs, "DB_PATH", tracker_db)
    return tracker_db


@pytest.fixture()
def users_root(tmp_path, monkeypatch):
    root = tmp_path / "users"
    monkeypatch.setattr("hunter.config.USERS_ROOT", root)
    return root


def _insert_job(db, *, kind: str, user_id: str, payload: str, status: str = "pending") -> str:
    from datetime import datetime, timezone

    job_id = str(uuid.uuid4())
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO profile_jobs (id, user_id, kind, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                job_id,
                user_id,
                kind,
                payload,
                status,
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )
    return job_id


def _row(db, job_id: str) -> dict:
    with get_db(db) as conn:
        row = conn.execute("SELECT * FROM profile_jobs WHERE id=?", (job_id,)).fetchone()
    return dict(row)


class TestResolveUserRelativePath:
    def test_resolves_a_normal_relative_path(self, users_root):
        (users_root / "u1").mkdir(parents=True)
        resolved = _resolve_user_relative_path("u1", "uploads/resume.docx")
        assert resolved == (users_root / "u1" / "uploads" / "resume.docx").resolve()

    def test_rejects_parent_traversal(self, users_root):
        with pytest.raises(ValueError):
            _resolve_user_relative_path("u1", "../u2/candidate/candidate.yaml")

    def test_rejects_absolute_path(self, users_root):
        # An absolute right-hand side silently discards the left side under
        # plain Path.__truediv__ — this must be rejected before any join.
        absolute = str((users_root / "u2" / "secret.docx").resolve())
        with pytest.raises(ValueError):
            _resolve_user_relative_path("u1", absolute)

    def test_rejects_empty_path(self, users_root):
        with pytest.raises(ValueError):
            _resolve_user_relative_path("u1", "")

    def test_nested_traversal_inside_a_deeper_path_is_rejected(self, users_root):
        with pytest.raises(ValueError):
            _resolve_user_relative_path("u1", "uploads/../../u2/candidate/candidate.yaml")


class TestRunRenderJob:
    def test_writes_rendered_files_and_profile_json(self, users_root):
        payload = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
        result = _run_render_job("u1", payload)

        written = json.loads(result)
        names = {Path(p).name for p in written}
        assert "candidate.yaml" in names
        assert "candidate_profile.md" in names
        assert "base_cv_angular.md" in names
        assert "profile.json" in names

        candidate_dir = users_root / "u1" / "candidate"
        assert (candidate_dir / "candidate.yaml").exists()
        profile_json = json.loads((candidate_dir / "profile.json").read_text(encoding="utf-8"))
        assert profile_json["core"]["identity"]["full_name"] == "Jane Doe"

    def test_malformed_json_payload_raises(self, users_root):
        with pytest.raises(ValueError):
            _run_render_job("u1", "not json")


class TestRunParseJob:
    def test_uses_injected_llm_and_returns_profile_json(self, users_root, monkeypatch):
        def fake_call_llm(**kwargs):
            return {
                "core": {
                    "identity": {
                        "full_name": "Parsed Name",
                        "contact": "parsed@example.com",
                        "cv_filename_prefix": "Parsed_Name_CV",
                    }
                },
                "leftovers": [],
            }

        monkeypatch.setattr("llm_client.call_llm", fake_call_llm)

        upload_dir = users_root / "u1" / "uploads"
        upload_dir.mkdir(parents=True)
        (upload_dir / "resume.txt").write_text(
            "Parsed Name\nparsed@example.com\n", encoding="utf-8"
        )

        result = sched._run_parse_job("u1", "uploads/resume.txt")
        profile = json.loads(result)
        assert profile["core"]["identity"]["full_name"] == "Parsed Name"

    def test_llm_failure_falls_back_to_leftovers_not_raises(self, users_root, monkeypatch):
        def broken_llm(**kwargs):
            raise RuntimeError("model unavailable")

        monkeypatch.setattr("llm_client.call_llm", broken_llm)

        upload_dir = users_root / "u1" / "uploads"
        upload_dir.mkdir(parents=True)
        (upload_dir / "resume.txt").write_text("Some resume text.", encoding="utf-8")

        result = sched._run_parse_job("u1", "uploads/resume.txt")
        profile = json.loads(result)
        assert profile["leftovers"]
        assert "Some resume text." in profile["leftovers"][0]["text"]

    def test_unsafe_path_raises_before_touching_files(self, users_root, monkeypatch):
        calls = []
        monkeypatch.setattr(
            "hunter.profile_parse.extract_resume_text", lambda p: calls.append(p) or "text"
        )
        with pytest.raises(ValueError):
            sched._run_parse_job("u1", "../u2/uploads/resume.txt")
        assert calls == []


def _fake_generate_docs_run(*, exit_code: int = 0):
    """Stand-in for `subprocess.run([python, generate_docs.py, content.json,
    "--no-tracker"])` — mirrors tests/test_golden_apply_e2e.py's
    FakeGenerateDocsRunner / tests/test_profile_preview.py's own fake,
    without spawning a real subprocess or LibreOffice. Records every
    invocation's argv so a test can assert `--no-tracker` was actually
    passed, not just that no tracker row happened to appear."""
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(list(cmd))
        content = json.loads(Path(cmd[2]).read_text(encoding="utf-8"))
        output_folder = Path(content["output_folder"])
        output_folder.mkdir(parents=True, exist_ok=True)
        if exit_code == 0:
            (output_folder / "Preview_CV_EN.pdf").write_bytes(b"%PDF-1.4 fake")
        return subprocess.CompletedProcess(
            cmd, exit_code, stdout="", stderr="boom" if exit_code else ""
        )

    _run.calls = calls
    return _run


def _write_candidate_yaml(users_root: Path, user_id: str) -> None:
    candidate_dir = users_root / user_id / "candidate"
    candidate_dir.mkdir(parents=True, exist_ok=True)
    (candidate_dir / "candidate.yaml").write_text(
        "identity:\n  full_name: Test Candidate\n  contact: test@example.com\n"
        "  cv_filename_prefix: Test_CV\n",
        encoding="utf-8",
    )


class TestRunPreviewJob:
    def test_writes_pdf_into_a_dated_subfolder_and_returns_pdf_first(self, users_root, monkeypatch):
        _write_candidate_yaml(users_root, "u1")
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        payload = json.dumps({"profile": {"core": {"summary": "hi"}}, "track": "core"})
        result = _run_preview_job("u1", payload)

        written = json.loads(result)
        assert written, "expected at least the PDF"
        assert written[0].endswith(".pdf")
        preview_root = users_root / "u1" / "candidate" / "preview" / "core"
        assert preview_root.is_dir()
        run_dirs = list(preview_root.iterdir())
        assert len(run_dirs) == 1  # one dated subfolder for this run

    def test_no_tracker_flag_is_actually_passed(self, users_root, monkeypatch):
        _write_candidate_yaml(users_root, "u1")
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        payload = json.dumps({"profile": {}, "track": "core"})
        _run_preview_job("u1", payload)

        assert "--no-tracker" in fake.calls[0]

    def test_no_tracker_row_is_written(self, users_root, monkeypatch, jobs_db):
        _write_candidate_yaml(users_root, "u1")
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        payload = json.dumps({"profile": {}, "track": "core"})
        _run_preview_job("u1", payload)

        with get_db(jobs_db) as conn:
            count = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
        assert count == 0

    def test_missing_candidate_yaml_raises_publish_first_error(self, users_root, monkeypatch):
        # No candidate.yaml written for u1 — profile was never published.
        calls = []
        monkeypatch.setattr(
            "hunter.profile_preview.subprocess.run", lambda *a, **k: calls.append(1)
        )

        payload = json.dumps({"profile": {}, "track": "core"})
        with pytest.raises(ValueError, match="publish"):
            _run_preview_job("u1", payload)

        assert calls == []
        assert not (users_root / "u1" / "candidate" / "preview").exists()

    def test_unsafe_track_raises_before_touching_files(self, users_root, monkeypatch):
        _write_candidate_yaml(users_root, "u1")
        calls = []
        monkeypatch.setattr(
            "hunter.profile_preview.subprocess.run", lambda *a, **k: calls.append(1)
        )

        payload = json.dumps({"profile": {}, "track": "../escape"})
        with pytest.raises(profile_preview.PreviewError):
            _run_preview_job("u1", payload)

        assert calls == []
        assert not (users_root / "u1" / "candidate" / "preview").exists()

    def test_malformed_json_payload_raises(self, users_root):
        with pytest.raises(ValueError):
            _run_preview_job("u1", "not json")

    def test_two_runs_for_the_same_track_each_get_their_own_dated_subfolder(
        self, users_root, monkeypatch
    ):
        _write_candidate_yaml(users_root, "u1")
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)

        payload = json.dumps({"profile": {}, "track": "core"})
        _run_preview_job("u1", payload)
        _run_preview_job("u1", payload)

        preview_root = users_root / "u1" / "candidate" / "preview" / "core"
        assert len(list(preview_root.iterdir())) == 2


class TestDrainOnce:
    def test_processes_a_pending_render_job_to_done(self, jobs_db, users_root):
        payload = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
        job_id = _insert_job(jobs_db, kind="render", user_id="u1", payload=payload)

        processed = drain_once()

        assert processed == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "done"
        written = json.loads(row["result"])
        assert any(p.endswith("candidate.yaml") for p in written)
        assert (users_root / "u1" / "candidate" / "candidate.yaml").exists()

    def test_unknown_kind_fails_the_job_without_raising(self, jobs_db, users_root):
        job_id = _insert_job(jobs_db, kind="bogus", user_id="u1", payload="{}")
        processed = drain_once()
        assert processed == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "error"
        assert "bogus" in row["error"]

    def test_unsafe_parse_path_fails_the_job_without_raising(self, jobs_db, users_root):
        job_id = _insert_job(
            jobs_db, kind="parse", user_id="u1", payload="../u2/candidate/candidate.yaml"
        )
        processed = drain_once()
        assert processed == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "error"

    def test_processes_a_pending_preview_job_to_done(self, jobs_db, users_root, monkeypatch):
        _write_candidate_yaml(users_root, "u1")
        fake = _fake_generate_docs_run()
        monkeypatch.setattr("hunter.profile_preview.subprocess.run", fake)
        payload = json.dumps({"profile": {}, "track": "core"})
        job_id = _insert_job(jobs_db, kind="preview", user_id="u1", payload=payload)

        processed = drain_once()

        assert processed == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "done"
        written = json.loads(row["result"])
        assert written and written[0].endswith(".pdf")

    def test_preview_job_without_published_profile_fails_the_job(self, jobs_db, users_root):
        payload = json.dumps({"profile": {}, "track": "core"})
        job_id = _insert_job(jobs_db, kind="preview", user_id="u1", payload=payload)

        processed = drain_once()

        assert processed == 1
        row = _row(jobs_db, job_id)
        assert row["status"] == "error"
        assert "publish" in row["error"]

    def test_drains_multiple_pending_jobs_in_one_call(self, jobs_db, users_root):
        payload = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
        _insert_job(jobs_db, kind="render", user_id="u1", payload=payload)
        _insert_job(jobs_db, kind="render", user_id="u2", payload=payload)
        assert drain_once() == 2
        assert drain_once() == 0  # queue now empty

    def test_stale_running_job_is_reset_then_reprocessed(self, jobs_db, users_root):
        from datetime import datetime, timedelta, timezone

        payload = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
        job_id = _insert_job(
            jobs_db, kind="render", user_id="u1", payload=payload, status="running"
        )
        stale = (datetime.now(timezone.utc) - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        with get_db(jobs_db) as conn:
            conn.execute("UPDATE profile_jobs SET updated_at=? WHERE id=?", (stale, job_id))

        processed = drain_once(timeout_min=10)
        assert processed == 1
        assert _row(jobs_db, job_id)["status"] == "done"


@pytest.fixture()
def isolated_best_effort(tmp_path, monkeypatch):
    """best_effort() counts failures in its OWN SQLite DB_PATH (a separate
    module-level symbol from tracker/profile_jobs) — point it at a temp DB
    and stub notify so a test never writes into the repo-local tracker.db or
    hits Telegram (mirrors tests/test_pl_resume_mirror.py's own precedent)."""
    from hunter import best_effort as be

    monkeypatch.setattr(be, "DB_PATH", tmp_path / "best_effort.db")
    monkeypatch.setattr(be, "_default_notify", lambda *a, **k: None)


class TestScheduledProfileJobsDrain:
    """The async JobQueue callback: registration + best_effort wrapping."""

    def test_registered_in_schedules_package(self):
        from hunter import schedules

        assert callable(schedules.scheduled_profile_jobs_drain)

    def test_drains_the_queue_when_invoked(self, jobs_db, users_root, isolated_best_effort):
        import asyncio

        payload = EXAMPLE_PROFILE_PATH.read_text(encoding="utf-8")
        job_id = _insert_job(jobs_db, kind="render", user_id="u1", payload=payload)

        asyncio.run(sched.scheduled_profile_jobs_drain(None))

        assert _row(jobs_db, job_id)["status"] == "done"

    def test_unexpected_drain_failure_does_not_propagate(
        self, jobs_db, users_root, isolated_best_effort, monkeypatch
    ):
        """A failure OUTSIDE a single job (e.g. the claim query itself) must
        not crash the JobQueue tick — best_effort swallows it, same contract
        as every other best-effort subsystem in this codebase."""
        import asyncio

        def broken_reset(*_a, **_kw):
            raise RuntimeError("db is on fire")

        monkeypatch.setattr(profile_jobs, "reset_stale_profile_jobs", broken_reset)

        asyncio.run(sched.scheduled_profile_jobs_drain(None))  # must not raise
