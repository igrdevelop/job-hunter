"""hunter/commands/url_message.py::_handle_apply routes linkedin_scout_relay
jobs through the paste flow (no fetchable URL — see
hunter/sources/linkedin_scout_relay.py), instead of the normal URL-based
apply_agent invocation.
"""

import asyncio
from pathlib import Path
from unittest.mock import patch

from hunter.commands.url_message import _handle_apply
from hunter.models import Job


def _run(coro):
    return asyncio.run(coro)


class _FakeMessage:
    def __init__(self):
        self.text = "some card text"
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)


class _FakeQuery:
    def __init__(self):
        self.message = _FakeMessage()
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append(text)


def test_relay_job_apply_uses_paste_file(tmp_path):
    job = Job(
        title="[LI post] hiring",
        company="Deloitte",
        location="",
        salary=None,
        url="https://linkedin.com/scout-posts/pabc123",
        source="linkedin_scout_relay",
        raw={"post_text": "We're hiring an Angular Developer, remote."},
    )
    query = _FakeQuery()
    captured_calls = []

    async def fake_run_apply_agent(url, force=False, paste_file=None, permalink=None):
        captured_calls.append((url, force, paste_file, permalink))

    async def scenario():
        await _handle_apply(query, job, "job_id_1", context=None)
        # _handle_apply schedules the real call via asyncio.create_task —
        # give the same event loop a tick to run it before the loop closes.
        await asyncio.sleep(0)

    with patch("hunter.commands.url_message._run_apply_agent", fake_run_apply_agent):
        _run(scenario())

    assert len(captured_calls) == 1
    url, force, paste_file, permalink = captured_calls[0]
    assert url == job.url
    assert paste_file is not None
    assert permalink is None
    saved_text = Path(paste_file).read_text(encoding="utf-8")
    assert "We're hiring an Angular Developer" in saved_text
    Path(paste_file).unlink(missing_ok=True)


def test_relay_job_apply_forwards_permalink(tmp_path):
    job = Job(
        title="[LI post] hiring",
        company="Deloitte",
        location="",
        salary=None,
        url="https://linkedin-scout.internal/posts/pabc123",
        source="linkedin_scout_relay",
        raw={
            "post_text": "We're hiring an Angular Developer, remote.",
            "permalink": "https://www.linkedin.com/posts/someone_activity-123",
        },
    )
    query = _FakeQuery()
    captured_calls = []

    async def fake_run_apply_agent(url, force=False, paste_file=None, permalink=None):
        captured_calls.append((url, force, paste_file, permalink))

    async def scenario():
        await _handle_apply(query, job, "job_id_3", context=None)
        await asyncio.sleep(0)

    with patch("hunter.commands.url_message._run_apply_agent", fake_run_apply_agent):
        _run(scenario())

    assert len(captured_calls) == 1
    _, _, paste_file, permalink = captured_calls[0]
    assert permalink == "https://www.linkedin.com/posts/someone_activity-123"
    Path(paste_file).unlink(missing_ok=True)


def test_normal_job_apply_does_not_use_paste_file():
    job = Job(
        title="Angular Dev",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://justjoin.it/job-offer/acme",
        source="justjoin",
    )
    query = _FakeQuery()
    captured_calls = []

    async def fake_run_apply_agent(url, force=False, paste_file=None, permalink=None):
        captured_calls.append((url, force, paste_file, permalink))

    async def scenario():
        await _handle_apply(query, job, "job_id_2", context=None)
        await asyncio.sleep(0)

    with patch("hunter.commands.url_message._run_apply_agent", fake_run_apply_agent):
        _run(scenario())

    assert len(captured_calls) == 1
    url, force, paste_file, permalink = captured_calls[0]
    assert url == job.url
    assert paste_file is None
    assert permalink is None
