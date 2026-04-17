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
