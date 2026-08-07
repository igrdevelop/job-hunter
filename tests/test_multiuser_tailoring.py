"""Multi-user Phase B3 item 5b — non-owner manual tailoring path."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from hunter import users
from hunter.db import get_db
from hunter.models import Job

ADMIN_CHAT = 777000
OWNER = "owner-uid"
OTHER = "other-uid"
OTHER_CHAT = 555111


def _job(**kwargs) -> Job:
    defaults = {
        "title": "Angular Dev",
        "company": "Acme",
        "location": "Remote",
        "salary": None,
        "url": "https://example.com/job/1",
        "source": "test",
    }
    defaults.update(kwargs)
    return Job(**defaults)


def _link(db, chat_id: int, user_id: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def _env(monkeypatch, tmp_path, tracker_db):
    users_root = tmp_path / "users"
    monkeypatch.setattr("hunter.config.USERS_ROOT", users_root)
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", OWNER)
    monkeypatch.setattr("hunter.config.TELEGRAM_CHAT_ID", ADMIN_CHAT)
    return users_root


def _make_candidate_yaml(users_root, user_id: str) -> None:
    cdir = users_root / user_id / "candidate"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "candidate.yaml").write_text("identity:\n  full_name: Test\n")


# ── tracker._uid subprocess seam ──────────────────────────────────────────────


def test_uid_env_var_wins_over_default(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setattr("hunter.tracker.DEFAULT_USER_ID", "the-owner")
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "subprocess-user")
    assert tracker._uid() == "subprocess-user"
    monkeypatch.delenv("JOB_HUNTER_USER_ID")
    assert tracker._uid() == "the-owner"


def test_uid_stamps_tracker_rows(tracker_db, monkeypatch):
    from hunter import tracker

    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    tracker.add_skipped(_job())
    with get_db(tracker_db) as conn:
        row = conn.execute("SELECT user_id FROM applications").fetchone()
    assert row["user_id"] == OTHER


def test_uid_scopes_dedup_per_user(tracker_db, monkeypatch):
    """Same URL for two users — two rows, no dedup collision."""
    from hunter import tracker

    monkeypatch.setenv("JOB_HUNTER_USER_ID", OTHER)
    assert tracker.add_skipped(_job()) is not None
    assert tracker.is_known(_job().url)
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "third-user")
    assert not tracker.is_known(_job().url)
    assert tracker.add_skipped(_job()) is not None
    with get_db(tracker_db) as conn:
        n = conn.execute("SELECT COUNT(*) c FROM applications").fetchone()["c"]
    assert n == 2


# ── users.user_env ────────────────────────────────────────────────────────────


def test_user_env_contents(monkeypatch, tmp_path):
    monkeypatch.setattr("hunter.config.USERS_ROOT", tmp_path / "users")
    env = users.user_env(OTHER, chat_id=OTHER_CHAT)
    assert env["JOB_HUNTER_USER_ID"] == OTHER
    assert env["CANDIDATE_YAML_PATH"].endswith("candidate.yaml")
    assert (tmp_path / "users" / OTHER / "Applications") == users.user_paths(OTHER).applications_dir
    assert env["APPLICATIONS_DIR"] == str(users.user_paths(OTHER).applications_dir)
    assert env["TELEGRAM_CHAT_ID"] == str(OTHER_CHAT)


def test_user_env_without_chat(monkeypatch, tmp_path):
    monkeypatch.setattr("hunter.config.USERS_ROOT", tmp_path / "users")
    env = users.user_env(OTHER)
    assert "TELEGRAM_CHAT_ID" not in env


# ── apply_service extra_env plumbing ──────────────────────────────────────────


def test_run_apply_agent_for_url_injects_env(monkeypatch, tmp_path):
    from hunter.services import apply_service

    captured: dict = {}

    async def fake_exec(*cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", fake_exec)
    outcome, _ = asyncio.run(
        apply_service.run_apply_agent_for_url(
            url="https://example.com/job/1",
            timeout_sec=5,
            apply_agent_path=tmp_path / "apply_agent.py",
            python_executable="python",
            extra_env={"JOB_HUNTER_USER_ID": OTHER, "TELEGRAM_CHAT_ID": str(OTHER_CHAT)},
        )
    )
    assert outcome == "ok"
    assert captured["env"]["JOB_HUNTER_USER_ID"] == OTHER
    assert captured["env"]["TELEGRAM_CHAT_ID"] == str(OTHER_CHAT)
    # full parent environ is preserved underneath the overlay
    assert len(captured["env"]) > 2


def test_run_apply_agent_for_url_no_env_by_default(monkeypatch, tmp_path):
    from hunter.services import apply_service

    captured: dict = {"env": "sentinel"}

    async def fake_exec(*cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        proc = MagicMock()
        proc.communicate = AsyncMock(return_value=(b"", b""))
        proc.returncode = 0
        return proc

    monkeypatch.setattr(apply_service.asyncio, "create_subprocess_exec", fake_exec)
    asyncio.run(
        apply_service.run_apply_agent_for_url(
            url="https://example.com/job/1",
            timeout_sec=5,
            apply_agent_path=tmp_path / "apply_agent.py",
            python_executable="python",
        )
    )
    assert captured["env"] is None  # inherit parent environment unchanged


# ── _tg_notify chat routing ───────────────────────────────────────────────────


def test_tg_notify_chat_override(monkeypatch):
    from hunter.bot import notifications

    sent: dict = {}

    class FakeBot:
        def __init__(self, token):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send_message(self, chat_id, text, **kwargs):
            sent["chat_id"] = chat_id

    monkeypatch.setattr(notifications, "Bot", FakeBot)
    asyncio.run(notifications._tg_notify("hi", chat_id=OTHER_CHAT))
    assert sent["chat_id"] == OTHER_CHAT


# ── cmd_url routing for non-owner callers ─────────────────────────────────────


def _update(chat_id: int, text: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def test_cmd_url_non_owner_routes_with_identity(tracker_db, monkeypatch, tmp_path):
    from hunter.commands import url_message

    users_root = _env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, OTHER_CHAT, OTHER)
    _make_candidate_yaml(users_root, OTHER)

    runs: list = []

    async def fake_run(url, **kwargs):
        runs.append((url, kwargs))

    monkeypatch.setattr(url_message, "_run_apply_agent", fake_run)
    update = _update(OTHER_CHAT, "https://example.com/job/9")
    asyncio.run(url_message.cmd_url(update, MagicMock()))
    # allow the created task to run
    assert len(runs) == 1 or _drain(runs)
    url, kwargs = runs[0]
    assert url == "https://example.com/job/9"
    assert kwargs["user_id"] == OTHER
    assert kwargs["user_chat_id"] == OTHER_CHAT


def _drain(runs, timeout=1.0):
    async def wait():
        for _ in range(50):
            if runs:
                return True
            await asyncio.sleep(0.02)
        return False

    return asyncio.run(wait())


def test_cmd_url_non_owner_without_candidate_yaml_blocked(tracker_db, monkeypatch, tmp_path):
    from hunter.commands import url_message

    _env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, OTHER_CHAT, OTHER)  # linked, but no candidate.yaml uploaded

    called = MagicMock()
    monkeypatch.setattr(url_message, "_run_apply_agent", called)
    update = _update(OTHER_CHAT, "https://example.com/job/9")
    asyncio.run(url_message.cmd_url(update, MagicMock()))
    called.assert_not_called()
    text = update.message.reply_text.await_args.args[0]
    assert "not set up" in text


def test_cmd_url_non_owner_linkedin_search_declined(tracker_db, monkeypatch, tmp_path):
    from hunter.commands import url_message

    users_root = _env(monkeypatch, tmp_path, tracker_db)
    _link(tracker_db, OTHER_CHAT, OTHER)
    _make_candidate_yaml(users_root, OTHER)

    batch = MagicMock()
    monkeypatch.setattr(url_message, "_run_linkedin_batch", batch)
    update = _update(
        OTHER_CHAT,
        "https://www.linkedin.com/jobs/search/?currentJobId=123456",
    )
    asyncio.run(url_message.cmd_url(update, MagicMock()))
    batch.assert_not_called()
    text = update.message.reply_text.await_args.args[0]
    assert "not supported" in text


def test_cmd_url_owner_path_unchanged(tracker_db, monkeypatch, tmp_path):
    """Admin chat: no user_id kwargs — the legacy owner flow."""
    from hunter.commands import url_message

    _env(monkeypatch, tmp_path, tracker_db)

    runs: list = []

    async def fake_run(url, **kwargs):
        runs.append((url, kwargs))

    monkeypatch.setattr(url_message, "_run_apply_agent", fake_run)
    update = _update(ADMIN_CHAT, "https://example.com/job/7")
    asyncio.run(url_message.cmd_url(update, MagicMock()))
    _drain(runs)
    assert runs
    url, kwargs = runs[0]
    assert url == "https://example.com/job/7"
    assert "user_id" not in kwargs
