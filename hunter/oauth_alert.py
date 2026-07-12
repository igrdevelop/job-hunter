"""
hunter/oauth_alert.py — detect Google OAuth token expiry and alert via Telegram.

When a Google refresh token is revoked/expired (`invalid_grant`), every Sheets /
Gmail / Drive call starts failing silently — and worse, a dead Sheets token once
caused a false-EXPIRED cascade (new applies couldn't be mirrored, then the next
pull's reconcile mistook never-mirrored rows for user deletions). A loud, early
"re-auth needed" alert beats discovering the damage later.

`refresh_or_alert()` wraps the `creds.refresh()` call at each client's auth
boundary: on an auth error it fires a (cooldown-deduplicated) Telegram alert
naming the service and the re-auth command, then re-raises so the caller's
existing best-effort handling proceeds unchanged.
"""

from __future__ import annotations

import logging
import time

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

# Substrings that mark an OAuth refresh/credential failure (vs a transient 5xx).
_AUTH_MARKERS = (
    "invalid_grant",
    "invalid_rapt",
    "token has been expired or revoked",
    "expired or revoked",
    "invalid_token",
    "is missing or invalid",
)

# Alert at most once per service within this window, so a 5-min pull loop hitting
# a dead token doesn't spam the chat.
_ALERT_COOLDOWN_SEC = 6 * 3600
_last_alert: dict[str, float] = {}


def is_oauth_error(exc: BaseException) -> bool:
    """True if `exc` looks like an OAuth refresh/credential failure."""
    if "refresherror" in type(exc).__name__.lower():
        return True
    msg = str(exc).lower()
    return any(m in msg for m in _AUTH_MARKERS)


def _send_telegram(text: str) -> bool:
    """Direct, dependency-light Telegram send (sync). Best-effort."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        import requests

        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        return resp.ok
    except Exception as e:  # noqa: BLE001
        log.warning("oauth_alert: Telegram send failed: %s", e)
        return False


def alert_oauth_expired(service: str, exc: BaseException, *, reauth_cmd: str) -> bool:
    """Send a deduplicated 're-auth needed' alert for `service`.

    Returns True if an alert was actually sent (False if suppressed by cooldown
    or no Telegram configured). Cooldown is per service name.
    """
    now = time.time()
    if now - _last_alert.get(service, 0.0) < _ALERT_COOLDOWN_SEC:
        return False
    _last_alert[service] = now
    sent = _send_telegram(
        f"🔑 <b>{service} token expired</b>\n"
        f"Google rejected the refresh token (likely revoked/expired), so "
        f"{service} stopped working. Re-authorize:\n"
        f"<code>{reauth_cmd}</code>\n\n"
        f"<i>{str(exc)[:200]}</i>"
    )
    log.error("oauth_alert: %s token expired (%s) — alert sent=%s", service, exc, sent)
    return sent


def refresh_or_alert(creds, request, token_file, *, service: str, reauth_cmd: str) -> None:
    """Refresh `creds` and persist the new token; on an OAuth error, fire a
    Telegram alert (deduped) then re-raise so existing handling is unchanged.

    Non-auth errors (transient network/5xx) propagate without alerting.
    """
    try:
        creds.refresh(request)
        token_file.write_text(creds.to_json())
    except Exception as e:  # noqa: BLE001 — classify, alert, re-raise
        if is_oauth_error(e):
            alert_oauth_expired(service, e, reauth_cmd=reauth_cmd)
        raise


def reset_cooldown() -> None:
    """Clear the per-service alert cooldown (test helper)."""
    _last_alert.clear()
