import asyncio

from hunter.main import _run_apply_agent
from hunter.models import Job


def test_run_apply_agent_delegates_to_apply_service(monkeypatch) -> None:
    job = Job(
        title="Senior Frontend Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://example.com/jobs/1",
        source="test",
    )

    captured: dict = {}

    async def _fake_runner(**kwargs):  # noqa: ANN003
        captured.update(kwargs)
        return "ok"

    monkeypatch.setattr("hunter.main.run_apply_agent_subprocess", _fake_runner)

    result = asyncio.run(_run_apply_agent(job))
    assert result == "ok"
    assert captured["job"].url == "https://example.com/jobs/1"
