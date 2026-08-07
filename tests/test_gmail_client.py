"""Tests for hunter/gmail_client.py — token scope handling.

Regression for the 2026-08-06 live incident: the labeling feature upgraded
SCOPES from gmail.readonly to gmail.modify, and `get_gmail_service()` forced
the new SCOPES onto the stored token — the refresh then requested a scope the
refresh token was never granted, and Google rejected it with
`invalid_scope: Bad Request`, killing the whole Gmail source. The client must
load the token with its OWN granted scopes and only degrade labeling.
"""

from __future__ import annotations

import json

import pytest

from hunter import gmail_client

READONLY = "https://www.googleapis.com/auth/gmail.readonly"
MODIFY = "https://www.googleapis.com/auth/gmail.modify"


def _write_token(tmp_path, scopes: list[str]):
    token = tmp_path / "gmail_token.json"
    token.write_text(
        json.dumps(
            {
                "token": "t",
                "refresh_token": "r",
                "client_id": "c",
                "client_secret": "s",
                "scopes": scopes,
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
    )
    return token


@pytest.fixture
def captured_build(monkeypatch):
    """Stub googleapiclient build; capture the credentials it receives."""
    captured = {}

    def _build(api, version, credentials=None):
        captured["credentials"] = credentials
        return object()

    monkeypatch.setattr(gmail_client, "build", _build)
    return captured


def test_old_readonly_token_is_not_forced_to_modify(tmp_path, monkeypatch, captured_build, caplog):
    """A pre-labeling token keeps its own scopes — no invalid_scope refresh."""
    monkeypatch.setattr(gmail_client, "TOKEN_PATH", _write_token(tmp_path, [READONLY]))
    with caplog.at_level("WARNING"):
        gmail_client.get_gmail_service()
    creds = captured_build["credentials"]
    assert creds.scopes == [READONLY]
    # Owner is told labeling is degraded, with the re-auth command.
    assert any(MODIFY in r.message and "gmail_auth" in r.message for r in caplog.records)


def test_modify_token_no_warning(tmp_path, monkeypatch, captured_build, caplog):
    monkeypatch.setattr(gmail_client, "TOKEN_PATH", _write_token(tmp_path, [MODIFY]))
    with caplog.at_level("WARNING"):
        gmail_client.get_gmail_service()
    assert captured_build["credentials"].scopes == [MODIFY]
    assert not any("labeling disabled" in r.message for r in caplog.records)


def test_missing_token_file_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(gmail_client, "TOKEN_PATH", tmp_path / "nope.json")
    with pytest.raises(FileNotFoundError):
        gmail_client.get_gmail_service()
