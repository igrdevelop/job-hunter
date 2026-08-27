"""
hunter/pipeline/notify.py — Telegram notification helpers for the apply
pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.

``notify()`` / ``send_telegram_documents()`` deliberately re-read
TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID / TELEGRAM_SEND_DOCS from
``hunter.apply_shared`` (not from hunter.config) at call time: that module
remains the attribute tests monkeypatch (``tests/conftest.py``'s autouse
``_no_telegram`` fixture blanks the token/chat id there for every test), and
a plain module-level import here would silently stop observing that patch
once this function moved out of apply_shared.py.
"""

from __future__ import annotations

import re
from pathlib import Path

import requests

# Formatting tags this module itself puts into notify() messages — stripped
# for the plain-text fallback resend below.
_NOTIFY_TAG_RE = re.compile(r"</?(?:b|i|u|s|a|code|pre)(?:\s[^<>]*)?>")


def notify(message: str) -> None:
    from hunter.apply_shared import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    api_url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            api_url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        if resp.status_code == 200:
            return
        # Telegram rejects the WHOLE message (400 "can't parse entities") when
        # interpolated content — an LLM error snippet, a quoted posting line —
        # breaks HTML parsing. That silently ate failure notifications: the
        # owner saw a bare "apply_agent failed" with no reason (2026-07-11).
        # Resend once as plain text with our own formatting tags stripped.
        print(
            f"[apply_agent] Telegram rejected HTML message (HTTP {resp.status_code}) — resending plain"
        )
        requests.post(
            api_url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": _NOTIFY_TAG_RE.sub("", message),
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply_agent] Telegram error: {e}")


# Telegram Bot API: max document size 50MB
_TELEGRAM_DOC_MAX_BYTES = 50 * 1024 * 1024
_TELEGRAM_SEND_DOC_TIMEOUT = 120


def send_telegram_documents(paths: list[Path]) -> None:
    """Send generated files to Telegram as documents (separate from notify text)."""
    from hunter.apply_shared import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_SEND_DOCS, notify

    if not TELEGRAM_SEND_DOCS or not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    if not paths:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    failed: list[str] = []
    sent = 0
    for p in sorted(paths, key=lambda x: x.name):
        if not p.is_file():
            continue
        try:
            size = p.stat().st_size
            if size > _TELEGRAM_DOC_MAX_BYTES:
                print(f"[apply_agent] Skipping Telegram doc (over 50MB): {p.name}")
                failed.append(f"{p.name} (over 50MB cap)")
                continue
            with p.open("rb") as f:
                r = requests.post(
                    url,
                    data={"chat_id": TELEGRAM_CHAT_ID},
                    files={"document": (p.name, f, "application/octet-stream")},
                    timeout=_TELEGRAM_SEND_DOC_TIMEOUT,
                )
            data = r.json() if r.content else {}
            if r.status_code != 200 or not data.get("ok"):
                desc = data.get("description", r.text[:200])
                print(f"[apply_agent] sendDocument failed for {p.name}: {desc}")
                failed.append(p.name)
            else:
                sent += 1
        except Exception as e:
            print(f"[apply_agent] sendDocument error for {p.name}: {e}")
            failed.append(p.name)
    if failed:
        short = "\n".join(f"  • {x}" for x in failed[:15])
        more = f"\n  … +{len(failed) - 15} more" if len(failed) > 15 else ""
        notify(f"⚠️ <b>Some files were not sent to Telegram</b>\n{short}{more}")
    elif sent:
        print(f"[apply_agent] Sent {sent} file(s) to Telegram")
