import asyncio
from pathlib import Path

from hunter.models import Job
from hunter.services.apply_service import build_generate_docs_cmd, run_apply_agent_subprocess


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True


def _job(url: str) -> Job:
    return Job(
        title="Senior Frontend Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url=url,
        source="test",
    )


def test_build_generate_docs_cmd_builds_expected_args() -> None:
    cmd = build_generate_docs_cmd(
        generate_docs_script=Path("D:/LearningProject/Claude/generate_docs.py"),
        content_json_path=Path("D:/tmp/Applications/2026-04-16/Acme/content.json"),
        use_full=True,
        force=True,
        python_executable="python",
    )
    assert cmd == [
        "python",
        str(Path("D:/LearningProject/Claude/generate_docs.py")),
        str(Path("D:/tmp/Applications/2026-04-16/Acme/content.json")),
        "--full",
        "--force",
    ]


def test_run_apply_agent_subprocess_returns_true_on_success(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            _job("https://example.com/jobs/1"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "ok"


def test_run_apply_agent_subprocess_returns_false_on_nonzero_exit(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(returncode=1, stderr=b"boom")

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            _job("https://example.com/jobs/2"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "fail"


def test_run_apply_agent_subprocess_times_out_and_kills_process(monkeypatch) -> None:
    class _SlowProc(_FakeProc):
        def __init__(self) -> None:
            super().__init__(returncode=0)
            self._calls = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            self._calls += 1
            if self._calls == 1:
                await asyncio.sleep(0.05)
            return b"", b""

    proc = _SlowProc()

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return proc

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            _job("https://example.com/jobs/3"),
            timeout_sec=0.01,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "fail"
    assert proc.killed is True


def test_run_apply_agent_subprocess_returns_false_on_oserror(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        raise OSError("cannot spawn process")

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            _job("https://example.com/jobs/4"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "fail"


def test_run_apply_agent_subprocess_returns_rate_limited_on_exit_45(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(returncode=45)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            _job("https://www.pracuj.pl/praca/x,oferta,1"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "rate_limited"


def test_run_apply_agent_subprocess_passes_title_for_non_jobleads_job(monkeypatch) -> None:
    """docs/DOOMED_GATE_PASTE_PLAN.md: --company/--title used to be JobLeads-only;
    now passed for any auto-hunt job with a known title, so the doomed gate's
    title-based checks see the real listing title instead of guessing one."""
    captured = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    asyncio.run(
        run_apply_agent_subprocess(
            _job("https://www.linkedin.com/jobs/view/123"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert "--company" in captured["args"]
    assert "--title" in captured["args"]
    idx = captured["args"].index("--title")
    assert captured["args"][idx + 1] == "Senior Frontend Developer"


def test_run_apply_agent_subprocess_omits_title_flags_when_unknown(monkeypatch) -> None:
    """A Job with no title/company at all (shouldn't normally happen for a
    real hunt job, but keeps the flag optional rather than sending 'Unknown')."""
    captured = {}

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured["args"] = args
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    blank = Job(
        title="",
        company="",
        location="",
        salary=None,
        url="https://example.com/jobs/2",
        source="test",
    )
    asyncio.run(
        run_apply_agent_subprocess(
            blank,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert "--company" not in captured["args"]
    assert "--title" not in captured["args"]


def test_run_apply_agent_subprocess_returns_manual_on_exit_44(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(program, *args, **kwargs):  # noqa: ANN002, ANN003
        assert "--company" in args
        return _FakeProc(returncode=44)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    jl = Job(
        title="Angular Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://www.jobleads.com/pl/job/test--poland--abc123deadbeef000000000000000",
        source="test",
    )

    result = asyncio.run(
        run_apply_agent_subprocess(
            jl,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )
    assert result == "manual"


def test_run_apply_agent_subprocess_does_not_swallow_unexpected_errors(monkeypatch) -> None:
    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("unexpected bug")

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    try:
        asyncio.run(
            run_apply_agent_subprocess(
                _job("https://example.com/jobs/5"),
                timeout_sec=1,
                apply_agent_path=Path("apply_agent.py"),
                python_executable="python",
            )
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("RuntimeError should bubble up for unexpected failures")


def _job_with_paste_text(url: str, post_text: str) -> Job:
    return Job(
        title="",
        company="Deloitte",
        location="",
        salary=None,
        url=url,
        source="linkedin_scout_relay",
        raw={"post_text": post_text},
    )


def test_run_apply_agent_subprocess_uses_paste_file_when_post_text_present(monkeypatch) -> None:
    captured_cmds = []

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured_cmds.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    job = _job_with_paste_text(
        "https://linkedin.com/scout-posts/pabc", "We're hiring an Angular Developer."
    )
    result = asyncio.run(
        run_apply_agent_subprocess(
            job,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )

    assert result == "ok"
    assert len(captured_cmds) == 1
    cmd = captured_cmds[0]
    assert "--paste-file" in cmd
    paste_path = Path(cmd[cmd.index("--paste-file") + 1])
    # temp file is cleaned up after the subprocess finishes — content already
    # verified via a separate direct write-check below, so just prove no leak.
    assert not paste_path.exists()


def test_run_apply_agent_subprocess_paste_file_contains_post_text(monkeypatch) -> None:
    written_paths = []

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        cmd = args
        idx = cmd.index("--paste-file")
        path = Path(cmd[idx + 1])
        written_paths.append(path.read_text(encoding="utf-8"))
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    job = _job_with_paste_text(
        "https://linkedin.com/scout-posts/pxyz", "We're hiring an Angular Developer, remote."
    )
    asyncio.run(
        run_apply_agent_subprocess(
            job,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )

    assert written_paths == ["We're hiring an Angular Developer, remote."]


def test_run_apply_agent_subprocess_passes_permalink_when_present(monkeypatch) -> None:
    captured_cmds = []

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured_cmds.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    job = Job(
        title="",
        company="Deloitte",
        location="",
        salary=None,
        url="https://linkedin-scout.internal/posts/pabc",
        source="linkedin_scout_relay",
        raw={
            "post_text": "We're hiring an Angular Developer.",
            "permalink": "https://www.linkedin.com/posts/someone_activity-123",
        },
    )
    asyncio.run(
        run_apply_agent_subprocess(
            job,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )

    cmd = captured_cmds[0]
    assert "--permalink" in cmd
    assert (
        cmd[cmd.index("--permalink") + 1] == "https://www.linkedin.com/posts/someone_activity-123"
    )


def test_run_apply_agent_subprocess_omits_permalink_when_absent(monkeypatch) -> None:
    captured_cmds = []

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured_cmds.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    job = _job_with_paste_text(
        "https://linkedin-scout.internal/posts/pnolink", "We're hiring an Angular Developer."
    )
    asyncio.run(
        run_apply_agent_subprocess(
            job,
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )

    assert "--permalink" not in captured_cmds[0]


def test_run_apply_agent_subprocess_no_paste_file_for_normal_job(monkeypatch) -> None:
    captured_cmds = []

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        captured_cmds.append(args)
        return _FakeProc(returncode=0)

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )

    asyncio.run(
        run_apply_agent_subprocess(
            _job("https://example.com/jobs/6"),
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )

    assert "--paste-file" not in captured_cmds[0]


# ── run_apply_agent_for_url failure detail (owner report 2026-07-11) ──────────
# apply_agent prints its diagnostics to STDOUT ("[apply_agent] LLM ERROR: …")
# before sys.exit(1); stderr is usually empty on those paths. The Telegram
# failure message used to show an unactionable "(no stderr)".


def _run_for_url(monkeypatch, proc: "_FakeProc"):
    from hunter.services.apply_service import run_apply_agent_for_url

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return proc

    monkeypatch.setattr(
        "hunter.services.apply_service.asyncio.create_subprocess_exec",
        _fake_create_subprocess_exec,
    )
    return asyncio.run(
        run_apply_agent_for_url(
            url="https://example.com/jobs/7",
            timeout_sec=1,
            apply_agent_path=Path("apply_agent.py"),
            python_executable="python",
        )
    )


def test_run_apply_agent_for_url_falls_back_to_stdout_on_empty_stderr(monkeypatch) -> None:
    outcome, detail = _run_for_url(
        monkeypatch,
        _FakeProc(returncode=1, stdout=b"[apply_agent] LLM ERROR: boom\n", stderr=b""),
    )
    assert outcome == "fail"
    assert "LLM ERROR: boom" in detail
    assert "(no stderr)" not in detail


def test_run_apply_agent_for_url_prefers_stderr_when_present(monkeypatch) -> None:
    outcome, detail = _run_for_url(
        monkeypatch,
        _FakeProc(returncode=1, stdout=b"stdout noise", stderr=b"Traceback: real error"),
    )
    assert outcome == "fail"
    assert detail == "Traceback: real error"


def test_run_apply_agent_for_url_reports_no_output_when_both_streams_empty(monkeypatch) -> None:
    outcome, detail = _run_for_url(monkeypatch, _FakeProc(returncode=1))
    assert outcome == "fail"
    assert detail == "(no output)"
