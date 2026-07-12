"""_auto_apply_all's pre-apply Telegram notification surfaces a scout post's
real permalink (job.raw["permalink"]) when present — owner request 2026-07-08
("на будущее ссылки копировались"). Source-agnostic: any Job with a
raw["permalink"] gets the extra line, not just linkedin_scout_relay ones.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import hunter.main as main
from hunter.models import Job


def _job(**raw_overrides) -> Job:
    return Job(
        title="Angular Developer",
        company="Acme",
        location="Remote",
        salary=None,
        url="https://linkedin.com/scout-posts/pabc123",
        source="linkedin_scout_relay",
        raw={"post_text": "We're hiring", **raw_overrides},
    )


def test_auto_apply_all_shows_permalink_when_present():
    job = _job(permalink="https://www.linkedin.com/feed/update/urn:li:share:1/")
    sent_texts = []

    async def fake_send_text(_ctx, text, **_kw):
        sent_texts.append(text)

    with (
        patch.object(main, "send_text", fake_send_text),
        patch.object(main, "_run_apply_agent", AsyncMock(return_value="ok")),
        patch.object(main, "_deliver_now", AsyncMock()),
    ):
        asyncio.run(main._auto_apply_all(None, [job]))

    pre_apply_text = sent_texts[0]
    assert "https://www.linkedin.com/feed/update/urn:li:share:1/" in pre_apply_text


def test_auto_apply_all_omits_permalink_line_when_absent():
    job = _job()  # no permalink key
    sent_texts = []

    async def fake_send_text(_ctx, text, **_kw):
        sent_texts.append(text)

    with (
        patch.object(main, "send_text", fake_send_text),
        patch.object(main, "_run_apply_agent", AsyncMock(return_value="ok")),
        patch.object(main, "_deliver_now", AsyncMock()),
    ):
        asyncio.run(main._auto_apply_all(None, [job]))

    pre_apply_text = sent_texts[0]
    assert "Post:" not in pre_apply_text
