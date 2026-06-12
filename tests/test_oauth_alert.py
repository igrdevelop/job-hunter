"""Tests for hunter/oauth_alert.py — Google OAuth token-expiry detection + alert."""

from __future__ import annotations

import pytest

from hunter import oauth_alert


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    oauth_alert.reset_cooldown()
    # Capture alerts instead of hitting Telegram.
    sent: list[str] = []
    monkeypatch.setattr(oauth_alert, "_send_telegram", lambda text: sent.append(text) or True)
    return sent


# ── is_oauth_error ────────────────────────────────────────────────────────────

def test_is_oauth_error_refresh_error_type():
    from google.auth.exceptions import RefreshError
    assert oauth_alert.is_oauth_error(RefreshError("invalid_grant: bad"))


def test_is_oauth_error_message_markers():
    assert oauth_alert.is_oauth_error(Exception("invalid_grant"))
    assert oauth_alert.is_oauth_error(Exception("Token has been expired or revoked."))
    assert oauth_alert.is_oauth_error(RuntimeError("gsheets_token.json is missing or invalid"))


def test_is_oauth_error_false_for_transient():
    assert not oauth_alert.is_oauth_error(Exception("503 Service Unavailable"))
    assert not oauth_alert.is_oauth_error(TimeoutError("connection timed out"))


# ── alert_oauth_expired (cooldown dedup) ──────────────────────────────────────

def test_alert_sends_once_then_cooldown(_reset):
    sent = _reset
    assert oauth_alert.alert_oauth_expired("Gmail", Exception("invalid_grant"),
                                           reauth_cmd="python tools/gmail_auth.py")
    # second call within cooldown is suppressed
    assert not oauth_alert.alert_oauth_expired("Gmail", Exception("invalid_grant"),
                                               reauth_cmd="python tools/gmail_auth.py")
    assert len(sent) == 1
    assert "Gmail token expired" in sent[0]
    assert "tools/gmail_auth.py" in sent[0]


def test_alert_per_service_independent(_reset):
    sent = _reset
    assert oauth_alert.alert_oauth_expired("Gmail", Exception("x"), reauth_cmd="a")
    assert oauth_alert.alert_oauth_expired("Google Sheets", Exception("x"), reauth_cmd="b")
    assert len(sent) == 2


def test_reset_cooldown_allows_resend(_reset):
    sent = _reset
    oauth_alert.alert_oauth_expired("Gmail", Exception("x"), reauth_cmd="a")
    oauth_alert.reset_cooldown()
    oauth_alert.alert_oauth_expired("Gmail", Exception("x"), reauth_cmd="a")
    assert len(sent) == 2


# ── refresh_or_alert ──────────────────────────────────────────────────────────

class _FakeCreds:
    def __init__(self, raise_exc=None):
        self._raise = raise_exc

    def refresh(self, _request):
        if self._raise:
            raise self._raise

    def to_json(self):
        return '{"token": "new"}'


def test_refresh_success_writes_token_no_alert(tmp_path, _reset):
    sent = _reset
    token = tmp_path / "token.json"
    oauth_alert.refresh_or_alert(
        _FakeCreds(), object(), token, service="Gmail", reauth_cmd="x"
    )
    assert token.read_text() == '{"token": "new"}'
    assert sent == []


def test_refresh_auth_error_alerts_and_reraises(tmp_path, _reset):
    from google.auth.exceptions import RefreshError
    sent = _reset
    token = tmp_path / "token.json"
    with pytest.raises(RefreshError):
        oauth_alert.refresh_or_alert(
            _FakeCreds(RefreshError("invalid_grant")), object(), token,
            service="Google Sheets", reauth_cmd="python tools/gsheets_auth.py",
        )
    assert len(sent) == 1
    assert "Google Sheets token expired" in sent[0]


def test_refresh_transient_error_reraises_no_alert(tmp_path, _reset):
    sent = _reset
    token = tmp_path / "token.json"
    with pytest.raises(ConnectionError):
        oauth_alert.refresh_or_alert(
            _FakeCreds(ConnectionError("network down")), object(), token,
            service="Gmail", reauth_cmd="x",
        )
    assert sent == []
