"""tests/test_url_policy.py — SSRF guard (docs/improvement-2026-09/
05-SECURITY_PLAN.md finding #6/M5).

Covers hunter.url_policy.validate_public_url directly, its wiring into
hunter.sources.html_fallback.fetch_html's manual redirect-following, and the
integration point in hunter.commands.url_message.cmd_url (Telegram URL
handler refuses instead of spawning an apply subprocess).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from hunter.url_policy import UrlPolicyError, validate_public_url


# ── validate_public_url — scheme / literal-host rejections ──────────────────


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data",  # cloud metadata endpoint
        "http://10.0.0.1/",  # RFC1918 private
        "http://172.16.5.5/",  # RFC1918 private
        "http://192.168.1.1/",  # RFC1918 private
        "http://127.0.0.1:8080/",  # loopback literal
        "http://localhost:3000/api/x",  # blocked hostname, no DNS needed
        "http://[::1]/",  # IPv6 loopback literal
        "http://[fe80::1]/",  # IPv6 link-local literal
        "file:///etc/passwd",  # disallowed scheme
        "ftp://x",  # disallowed scheme
        "gopher://10.0.0.1/",  # disallowed scheme
        "http://",  # no host at all
        "http://svc.internal/status",  # blocked internal suffix
        "http://box.local/",  # blocked .local suffix
    ],
)
def test_rejects_unsafe_url(url):
    with pytest.raises(UrlPolicyError):
        validate_public_url(url)


def test_rejects_unsafe_url_is_a_value_error():
    """UrlPolicyError subclasses ValueError, so existing `except ValueError` /
    `except Exception` handling around fetch calls needs no changes."""
    with pytest.raises(ValueError):
        validate_public_url("http://169.254.169.254/")


# ── validate_public_url — public URLs pass, DNS made mockable ───────────────


def test_public_url_passes(monkeypatch):
    """A normal job-board URL resolves to a public address and is returned
    unchanged — behavior for real job URLs is byte-identical to before."""

    def fake_getaddrinfo(host, port):
        assert host == "www.example-jobs.test"
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)
    url = "https://www.example-jobs.test/job/123"
    assert validate_public_url(url) == url


def test_hostname_resolving_to_loopback_is_refused(monkeypatch):
    """A hostname whose DNS answer is a private/loopback address is refused
    even though the URL text itself looks like an ordinary public host —
    the classic DNS-rebinding SSRF bypass."""

    def fake_getaddrinfo(host, port):
        assert host == "evil.example.test"
        return [(2, 1, 6, "", ("127.0.0.1", 0))]

    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlPolicyError):
        validate_public_url("http://evil.example.test/x")


def test_dns_resolution_failure_does_not_raise(monkeypatch):
    """A hostname that fails to resolve at all is NOT flagged as unsafe —
    that is an ordinary network/typo error, left for the real fetch call to
    report on its own (see module docstring)."""
    import socket

    def fake_getaddrinfo(host, port):
        raise socket.gaierror("Name or service not known")

    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)
    url = "https://this-domain-does-not-resolve.example.test/job/1"
    assert validate_public_url(url) == url


def test_one_disallowed_address_among_several_rejects(monkeypatch):
    """A multi-A-record host is rejected if ANY resolved address is private —
    fail closed, not fail on the first/last answer only."""

    def fake_getaddrinfo(host, port):
        return [
            (2, 1, 6, "", ("8.8.8.8", 0)),
            (2, 1, 6, "", ("10.1.2.3", 0)),
        ]

    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UrlPolicyError):
        validate_public_url("http://multi.example.test/x")


# ── html_fallback.fetch_html — redirect chain revalidation ──────────────────


def _resp(status_code, location=None, text="", raise_ok=True):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Location": location} if location else {}
    resp.is_redirect = status_code in (301, 302, 303, 307, 308) and bool(location)
    resp.text = text
    resp.raise_for_status = (
        MagicMock() if raise_ok else MagicMock(side_effect=RuntimeError("http error"))
    )
    return resp


def test_fetch_html_follows_public_redirect(monkeypatch):
    """A redirect chain that stays public is followed transparently — no
    behavior change for a normal job-board redirect."""
    from hunter.sources import html_fallback

    calls = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(url)
        assert allow_redirects is False
        if url == "https://board.example.test/job/1":
            return _resp(302, location="https://board.example.test/job/1-final")
        return _resp(200, text="A" * 200)

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(html_fallback.requests, "get", fake_get)
    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)

    text = html_fallback.fetch_html("https://board.example.test/job/1")
    assert "A" * 100 in text
    assert calls == [
        "https://board.example.test/job/1",
        "https://board.example.test/job/1-final",
    ]


def test_fetch_html_refuses_redirect_to_private_address(monkeypatch):
    """A redirect off an already-public host onto a private address is
    refused before the second (private) request is ever made."""
    from hunter.sources import html_fallback

    calls = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        calls.append(url)
        assert allow_redirects is False
        return _resp(302, location="http://169.254.169.254/latest/meta-data")

    monkeypatch.setattr(html_fallback.requests, "get", fake_get)

    with pytest.raises(UrlPolicyError):
        html_fallback.fetch_html("https://board.example.test/job/1")

    # Only the first (public) hop was actually requested.
    assert calls == ["https://board.example.test/job/1"]


def test_fetch_html_caps_redirect_chain_length(monkeypatch):
    """More than MAX_REDIRECTS_FOLLOWED hops raises instead of looping."""
    from hunter.sources import html_fallback

    def fake_get(url, headers=None, timeout=None, allow_redirects=None):
        # Always redirect to a new (public-resolving) URL — infinite chain.
        n = int(url.rsplit("/", 1)[-1])
        return _resp(302, location=f"https://board.example.test/{n + 1}")

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr(html_fallback.requests, "get", fake_get)
    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)

    with pytest.raises(ValueError, match="Too many redirects"):
        html_fallback.fetch_html("https://board.example.test/0")


# ── hunter.sources.fetch_job_text — use_session entry point ─────────────────


def test_fetch_job_text_use_session_validates_url():
    from hunter.sources import fetch_job_text

    with pytest.raises(UrlPolicyError):
        fetch_job_text("http://169.254.169.254/latest/meta-data", use_session=True)


def test_fetch_job_text_without_use_session_is_unchanged(monkeypatch):
    """Bulk callers (use_session=False) are NOT guarded here — the html
    fallback path is exercised the same as before this change."""
    import hunter.sources as sources_pkg

    called = {}

    def fake_fetch_html(url):
        called["url"] = url
        return "job text " * 20

    monkeypatch.setattr("hunter.sources.html_fallback.fetch_html", fake_fetch_html)
    # Force the roster to find no matching source so the generic fallback runs.
    monkeypatch.setattr(sources_pkg, "_fetch_roster", lambda: [])

    text = sources_pkg.fetch_job_text("http://169.254.169.254/latest/meta-data")
    assert called["url"] == "http://169.254.169.254/latest/meta-data"
    assert text.startswith("job text")


# ── cmd_url — Telegram entry point refuses instead of applying ──────────────


def _update(chat_id: int, text: str) -> MagicMock:
    update = MagicMock()
    update.effective_chat.id = chat_id
    update.message.text = text
    update.message.reply_text = AsyncMock()
    update.callback_query = None
    return update


def test_cmd_url_refuses_unsafe_url(tracker_db, monkeypatch):
    from hunter.commands import url_message

    admin_chat = 424242
    monkeypatch.setattr("hunter.config.TELEGRAM_CHAT_ID", admin_chat)

    run_agent = MagicMock()
    monkeypatch.setattr(url_message, "_run_apply_agent", run_agent)
    monkeypatch.setattr(url_message, "_looks_like_paste", lambda text: False)

    update = _update(admin_chat, "http://169.254.169.254/latest/meta-data")
    asyncio.run(url_message.cmd_url(update, MagicMock()))

    run_agent.assert_not_called()
    reply_text = update.message.reply_text.await_args.args[0]
    assert "private" in reply_text.lower() or "internal" in reply_text.lower()


def test_cmd_url_allows_public_url(tracker_db, monkeypatch):
    """Regression guard: a normal public URL still reaches the apply launcher
    (unchanged behavior) once DNS is mocked to a public address."""
    from hunter.commands import url_message

    admin_chat = 424243
    monkeypatch.setattr("hunter.config.TELEGRAM_CHAT_ID", admin_chat)

    def fake_getaddrinfo(host, port):
        return [(2, 1, 6, "", ("8.8.8.8", 0))]

    monkeypatch.setattr("hunter.url_policy.socket.getaddrinfo", fake_getaddrinfo)

    runs: list = []

    async def fake_run(url, **kwargs):
        runs.append((url, kwargs))

    monkeypatch.setattr(url_message, "_run_apply_agent", fake_run)
    update = _update(admin_chat, "https://board.example.test/job/1")
    asyncio.run(url_message.cmd_url(update, MagicMock()))

    async def _drain():
        for _ in range(50):
            if runs:
                return
            await asyncio.sleep(0.02)

    asyncio.run(_drain())
    assert runs and runs[0][0] == "https://board.example.test/job/1"


def test_cmd_url_non_owner_unsafe_url_refused(tracker_db, monkeypatch, tmp_path):
    """The SSRF check also guards the non-owner (linked-account) branch —
    a private-address URL is refused regardless of caller identity."""
    from datetime import datetime, timezone

    from hunter.commands import url_message
    from hunter.db import get_db

    admin_chat = 424244
    other_chat = 900001
    other_user = "other-uid"
    users_root = tmp_path / "users"
    monkeypatch.setattr("hunter.config.TELEGRAM_CHAT_ID", admin_chat)
    monkeypatch.setattr("hunter.config.USERS_ROOT", users_root)
    monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner-uid")

    with get_db(tracker_db) as conn:
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (other_chat, other_user, datetime.now(timezone.utc).isoformat()),
        )
    cdir = users_root / other_user / "candidate"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / "candidate.yaml").write_text("identity:\n  full_name: Test\n")

    run_agent = MagicMock()
    monkeypatch.setattr(url_message, "_run_apply_agent", run_agent)

    update = _update(other_chat, "http://10.0.0.1/internal-job")
    asyncio.run(url_message.cmd_url(update, MagicMock()))

    run_agent.assert_not_called()
