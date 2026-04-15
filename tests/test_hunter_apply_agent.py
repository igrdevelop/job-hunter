import asyncio

from hunter.models import Job
from hunter.main import _run_apply_agent


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


def test_run_apply_agent_returns_true_on_success(monkeypatch) -> None:
    job = Job(
        title="Senior Frontend Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/jobs/1",
        source="test",
    )

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(returncode=0)

    monkeypatch.setattr("hunter.main.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(_run_apply_agent(job))
    assert result is True


def test_run_apply_agent_returns_false_on_nonzero_exit(monkeypatch) -> None:
    job = Job(
        title="Senior Frontend Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/jobs/2",
        source="test",
    )

    async def _fake_create_subprocess_exec(*args, **kwargs):  # noqa: ANN002, ANN003
        return _FakeProc(returncode=1, stderr=b"boom")

    monkeypatch.setattr("hunter.main.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)

    result = asyncio.run(_run_apply_agent(job))
    assert result is False


def test_run_apply_agent_times_out_and_kills_process(monkeypatch) -> None:
    job = Job(
        title="Senior Frontend Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/jobs/3",
        source="test",
    )

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

    monkeypatch.setattr("hunter.main.asyncio.create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr("hunter.main.APPLY_AGENT_TIMEOUT_SEC", 0.01)

    result = asyncio.run(_run_apply_agent(job))
    assert result is False
    assert proc.killed is True
