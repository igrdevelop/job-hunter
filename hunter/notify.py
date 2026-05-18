"""
hunter/notify.py — Telegram notification helpers used by apply_agent.

Two entry points:
  notify(message)                  — send a text message (HTML parse mode)
  send_telegram_documents(paths)   — upload files as Telegram documents
"""

from pathlib import Path

import requests

from hunter.config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, TELEGRAM_SEND_DOCS

_TELEGRAM_DOC_MAX_BYTES = 50 * 1024 * 1024
_TELEGRAM_SEND_DOC_TIMEOUT = 120

def notify(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply_agent] Telegram error: {e}")


def send_telegram_documents(paths: list[Path]) -> None:
    """Send generated files to Telegram as documents (separate from notify text)."""
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
