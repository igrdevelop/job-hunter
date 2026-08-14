"""hunter.apply_stdout_log — full subprocess transcript capture for every
apply_agent.py run (success included), so a post-hoc investigation has the
child's own print() trail available. See the GeckoDynamics case (2026-08-14)
in AGENT_LOG for why this exists: the parent process only logs the child's
stdout at DEBUG, which is never enabled in prod, so a successful run's
internal reasoning (e.g. Step 4.5's react-only-skip decision) was previously
unrecoverable. Tests use set_log_dir_for_tests to redirect at a tmp dir so
nothing touches the real logs/apply_stdout/ (conftest.py also does this
globally via an autouse fixture — these tests set it explicitly for clarity
and to double-check the hook itself works).
"""

from pathlib import Path

import pytest


@pytest.fixture
def stdout_log_dir(tmp_path):
    from hunter import apply_stdout_log

    d = tmp_path / "apply_stdout"
    apply_stdout_log.set_log_dir_for_tests(d)
    yield d
    apply_stdout_log.set_log_dir_for_tests(None)


def _read_only_file(d: Path) -> Path:
    files = list(d.glob("*.log"))
    assert len(files) == 1
    return files[0]


def test_writes_one_transcript_file_with_expected_content(stdout_log_dir):
    from hunter.apply_stdout_log import save_apply_stdout

    save_apply_stdout(
        url="https://example.com/jobs/1",
        company="Acme",
        title="Senior Frontend Developer",
        outcome="ok",
        exit_code=0,
        stdout=b"[apply_agent] Step 4.5: stack=React, angular-active=False\n",
        stderr=b"",
        duration_sec=12.345,
    )
    f = _read_only_file(stdout_log_dir)
    text = f.read_text(encoding="utf-8")
    assert "url=https://example.com/jobs/1" in text
    assert "company=Acme" in text
    assert "title=Senior Frontend Developer" in text
    assert "outcome=ok" in text
    assert "exit_code=0" in text
    assert "duration_sec=12.3" in text
    assert "Step 4.5: stack=React" in text
    assert "Acme" in f.name


def test_filename_falls_back_to_url_slug_when_no_company_or_title(stdout_log_dir):
    from hunter.apply_stdout_log import save_apply_stdout

    save_apply_stdout(
        url="https://example.com/jobs/42",
        outcome="fail",
        exit_code=1,
        stdout=b"",
        stderr=b"boom",
    )
    f = _read_only_file(stdout_log_dir)
    assert "example_com_jobs_42" in f.name


def test_handles_none_stdout_and_stderr(stdout_log_dir):
    from hunter.apply_stdout_log import save_apply_stdout

    save_apply_stdout(
        url="https://example.com/jobs/2",
        company="Beta",
        outcome="cli_timeout",
        exit_code=None,
        stdout=None,
        stderr=None,
    )
    f = _read_only_file(stdout_log_dir)
    text = f.read_text(encoding="utf-8")
    assert "outcome=cli_timeout" in text
    assert "exit_code=None" in text


def test_truncates_oversized_output(stdout_log_dir):
    from hunter.apply_stdout_log import _MAX_TEXT_CHARS, save_apply_stdout

    huge = b"x" * (_MAX_TEXT_CHARS + 1000)
    save_apply_stdout(
        url="https://example.com/jobs/3",
        company="Giant",
        outcome="ok",
        exit_code=0,
        stdout=huge,
        stderr=b"",
    )
    f = _read_only_file(stdout_log_dir)
    text = f.read_text(encoding="utf-8")
    stdout_section = text.split("--- STDOUT ---\n", 1)[1].split("\n--- STDERR ---", 1)[0]
    assert stdout_section == "x" * _MAX_TEXT_CHARS


def test_never_raises_on_write_failure(stdout_log_dir, monkeypatch):
    """Best-effort: a broken write must not bubble up into the apply pipeline."""
    from hunter import apply_stdout_log

    monkeypatch.setattr(
        apply_stdout_log,
        "_log_dir",
        lambda: (_ for _ in ()).throw(OSError("disk full")),
    )
    apply_stdout_log.save_apply_stdout(
        url="https://example.com/jobs/4",
        outcome="ok",
        exit_code=0,
        stdout=b"",
        stderr=b"",
    )  # no raise
    assert not stdout_log_dir.exists()


def test_prune_old_removes_stale_files_only(stdout_log_dir, monkeypatch):
    import time

    from hunter.apply_stdout_log import _RETENTION_DAYS, save_apply_stdout

    save_apply_stdout(
        url="https://example.com/old",
        company="Old",
        outcome="ok",
        exit_code=0,
        stdout=b"",
        stderr=b"",
    )
    old_file = _read_only_file(stdout_log_dir)
    stale_mtime = time.time() - (_RETENTION_DAYS + 1) * 86400
    import os

    os.utime(old_file, (stale_mtime, stale_mtime))

    save_apply_stdout(
        url="https://example.com/new",
        company="New",
        outcome="ok",
        exit_code=0,
        stdout=b"",
        stderr=b"",
    )

    remaining = {f.name for f in stdout_log_dir.glob("*.log")}
    assert old_file.name not in remaining
    assert len(remaining) == 1


def test_set_log_dir_for_tests_none_restores_project_dir(monkeypatch, tmp_path):
    """Passing None to the test hook falls back to hunter.config.PROJECT_DIR —
    prove the fallback path doesn't crash by pointing PROJECT_DIR at a tmp dir."""
    from hunter import apply_stdout_log

    monkeypatch.setattr("hunter.config.PROJECT_DIR", tmp_path)
    apply_stdout_log.set_log_dir_for_tests(None)
    try:
        d = apply_stdout_log._log_dir()
        assert d == tmp_path / "logs" / "apply_stdout"
        assert d.exists()
    finally:
        apply_stdout_log.set_log_dir_for_tests(None)
