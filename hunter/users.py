"""
hunter/users.py — Telegram chat ↔ web-account linking (multi-user Phase B3).

The web API generates 6-char link codes (`telegram_link_codes`, 10-minute
expiry, uppercase hex, ISO-8601 UTC `expires_at`); this module consumes them
and maintains `telegram_links` (chat_id ↔ user_id, strictly one-to-one).
Both tables are created by hunter.db's multi-user DDL mirror — the API owns
the authoritative schema (docs/MULTI_USER_UPDATE.md, shared contract).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from hunter.db import get_db

log = logging.getLogger(__name__)


def _db_path() -> Path:
    # Follow hunter.tracker's DB_PATH at call time so the test suite's
    # tracker_db fixture (which monkeypatches tracker.DB_PATH onto an
    # isolated temp DB) covers this module without a second patch point.
    from hunter import tracker

    return tracker.DB_PATH


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_expiry(raw: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def link_chat(chat_id: int, code: str) -> str | None:
    """Consume a link code and bind chat_id to its user_id.

    Returns the linked user_id, or None for an unknown/expired/blank code.
    The code is single-use: deleted on success AND on an expired hit.
    Re-linking is a move, not an error — a user linking from a new chat
    replaces their old chat row, and a chat linking to a new account
    replaces its old user row (chat_id is PK, user_id is UNIQUE).
    """
    normalized = (code or "").strip().upper()
    if not normalized:
        return None
    now = _utcnow()
    with get_db(_db_path()) as conn:
        row = conn.execute(
            "SELECT user_id, expires_at FROM telegram_link_codes WHERE code = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            return None
        conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (normalized,))
        expires = _parse_expiry(row["expires_at"])
        if expires is None or expires < now:
            log.info("link code %s rejected: expired at %s", normalized, row["expires_at"])
            return None
        user_id = row["user_id"]
        conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT OR REPLACE INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, now.isoformat()),
        )
    log.info("chat %s linked to user %s", chat_id, user_id)
    return user_id


def resolve_user(chat_id: int) -> str | None:
    """Return the user_id linked to chat_id, or None for an unbound chat."""
    with get_db(_db_path()) as conn:
        row = conn.execute(
            "SELECT user_id FROM telegram_links WHERE chat_id = ?", (chat_id,)
        ).fetchone()
    return row["user_id"] if row is not None else None


def resolve_chat(user_id: str) -> int | None:
    """Return the chat_id linked to user_id, or None if the user never linked."""
    with get_db(_db_path()) as conn:
        row = conn.execute(
            "SELECT chat_id FROM telegram_links WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["chat_id"] if row is not None else None


class UserPaths:
    """Per-user storage layout under USERS_ROOT (shared contract).

    users/{userId}/
      candidate/            candidate.yaml, candidate_profile.md, base_cv_*.md
      Applications/         generated docs, {YYYY-MM-DD}/{Company}[_N]/
      templates/            resume/cover-letter templates + manifest.json
    """

    def __init__(self, user_id: str):
        from hunter.config import USERS_ROOT

        self.user_id = user_id
        self.root = USERS_ROOT / user_id
        self.candidate_dir = self.root / "candidate"
        self.candidate_yaml = self.candidate_dir / "candidate.yaml"
        self.applications_dir = self.root / "Applications"
        self.templates_dir = self.root / "templates"


def user_paths(user_id: str) -> UserPaths:
    """Storage paths for user_id (pure computation — creates nothing)."""
    return UserPaths(user_id)


def user_env(user_id: str, chat_id: int | None = None) -> dict[str, str]:
    """Env overrides for a per-user apply subprocess (Phase B3 seam).

    - CANDIDATE_YAML_PATH / APPLICATIONS_DIR point the pipeline at the
      user's own identity and output tree;
    - JOB_HUNTER_USER_ID makes every tracker write/dedup check in the child
      stamp/scope this user (hunter.tracker._uid);
    - TELEGRAM_CHAT_ID (when chat_id given) redirects the pipeline's own
      notifications (docs-ready message, PDF uploads) to the user's chat —
      hunter.config reads it from the environment at import time, so the
      injection needs no pipeline code changes.
    """
    paths = user_paths(user_id)
    env = {
        "CANDIDATE_YAML_PATH": str(paths.candidate_yaml),
        "APPLICATIONS_DIR": str(paths.applications_dir),
        "JOB_HUNTER_USER_ID": user_id,
    }
    if chat_id is not None:
        env["TELEGRAM_CHAT_ID"] = str(chat_id)
    return env


def list_active_users() -> list[str]:
    """User ids eligible for the hunt fan-out.

    Active = Telegram-linked + candidate.yaml present + `hunting_enabled`
    per-user setting truthy. NOTE (B3 scope decision, docs/
    MULTI_USER_UPDATE.md): hunting is owner-only until B3.5 —
    `hunting_enabled` is treated as false for any user other than
    DEFAULT_USER_ID regardless of what user_settings says.
    """
    from hunter.config import DEFAULT_USER_ID, user_setting

    with get_db(_db_path()) as conn:
        linked = [r["user_id"] for r in conn.execute("SELECT user_id FROM telegram_links")]
    active: list[str] = []
    for uid in linked:
        if DEFAULT_USER_ID and uid != DEFAULT_USER_ID:
            continue  # B3: hunting owner-only; lifted in B3.5
        if not user_paths(uid).candidate_yaml.is_file():
            continue
        if user_setting(uid, "hunting_enabled", "true").strip().lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            continue
        active.append(uid)
    return active


def unlink_chat(chat_id: int) -> bool:
    """Remove the link for chat_id. Returns True if a link existed."""
    with get_db(_db_path()) as conn:
        cur = conn.execute("DELETE FROM telegram_links WHERE chat_id = ?", (chat_id,))
        removed = cur.rowcount > 0
    if removed:
        log.info("chat %s unlinked", chat_id)
    return removed
