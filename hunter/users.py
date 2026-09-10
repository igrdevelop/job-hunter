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
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from hunter.db import ensure_link_attempts_table, get_db

log = logging.getLogger(__name__)

# Per-chat /link brute-force limiter (docs/improvement-2026-09/
# 05-SECURITY_PLAN.md finding #4/M4): the API's 6-char link code is only
# ~24 bits of entropy, and /link had no attempt counter at all — a chat
# could hammer codes limited only by Telegram's own rate limit. >= this many
# FAILED attempts within LINK_ATTEMPT_WINDOW_MIN minutes refuses further
# attempts outright, without even querying telegram_link_codes.
LINK_ATTEMPT_LIMIT = 5
LINK_ATTEMPT_WINDOW_MIN = 10


class LinkResult(NamedTuple):
    """Outcome of a link_chat_with_details() call.

    user_id            — the linked user_id, or None on any failure
                          (blank code, unknown code, expired code, or the
                          chat is currently rate-limited).
    rate_limited        — True when this call was refused purely by the
                          attempt limiter, without touching telegram_link_codes.
    displaced_chat_id   — set only on a SUCCESSFUL link that moved an
                          EXISTING user from a different chat (chat_id is
                          UNIQUE per user_id) — the old chat to notify. None
                          on failure, and None when the user had no prior
                          chat or was already linked from this same chat.
    """

    user_id: str | None
    rate_limited: bool = False
    displaced_chat_id: int | None = None


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


def _attempts_exceeded(conn: sqlite3.Connection, chat_id: int, now: datetime) -> bool:
    """True when chat_id has already hit LINK_ATTEMPT_LIMIT failures inside
    the current (still-open) window."""
    row = conn.execute(
        "SELECT window_start, attempts FROM link_attempts WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if row is None:
        return False
    window_start = _parse_expiry(row["window_start"])
    if window_start is None:
        return False
    if now - window_start > timedelta(minutes=LINK_ATTEMPT_WINDOW_MIN):
        return False  # window has elapsed — the next failure starts a fresh one
    return row["attempts"] >= LINK_ATTEMPT_LIMIT


def _record_attempt_failure(conn: sqlite3.Connection, chat_id: int, now: datetime) -> None:
    """Increment chat_id's failure count, starting a fresh window if the
    previous one has expired (or none exists yet)."""
    row = conn.execute(
        "SELECT window_start, attempts FROM link_attempts WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    window_start = _parse_expiry(row["window_start"]) if row else None
    window_expired = window_start is None or now - window_start > timedelta(
        minutes=LINK_ATTEMPT_WINDOW_MIN
    )
    if row is None:
        conn.execute(
            "INSERT INTO link_attempts (chat_id, window_start, attempts) VALUES (?, ?, 1)",
            (chat_id, now.isoformat()),
        )
    elif window_expired:
        conn.execute(
            "UPDATE link_attempts SET window_start = ?, attempts = 1 WHERE chat_id = ?",
            (now.isoformat(), chat_id),
        )
    else:
        conn.execute(
            "UPDATE link_attempts SET attempts = attempts + 1 WHERE chat_id = ?",
            (chat_id,),
        )


def _clear_attempts(conn: sqlite3.Connection, chat_id: int) -> None:
    conn.execute("DELETE FROM link_attempts WHERE chat_id = ?", (chat_id,))


def link_chat(chat_id: int, code: str) -> str | None:
    """Consume a link code and bind chat_id to its user_id.

    Returns the linked user_id, or None for an unknown/expired/blank code —
    or when this chat is currently rate-limited (see LinkResult / M4). Thin
    wrapper around link_chat_with_details() kept for the existing simple
    call sites/tests that only care about the user_id.
    """
    return link_chat_with_details(chat_id, code).user_id


def link_chat_with_details(chat_id: int, code: str) -> LinkResult:
    """Full version of link_chat() — also reports rate-limiting and a
    displaced old chat to notify (docs/improvement-2026-09/
    05-SECURITY_PLAN.md finding #4/M4).

    The code is single-use: deleted on success AND on an expired hit.
    Re-linking is a move, not an error — a user linking from a new chat
    replaces their old chat row, and a chat linking to a new account
    replaces its old user row (chat_id is PK, user_id is UNIQUE). A failed
    attempt (blank/unknown/expired code, or an already-rate-limited chat)
    counts against the per-chat limiter; a successful link clears it.
    """
    normalized = (code or "").strip().upper()
    now = _utcnow()
    with get_db(_db_path()) as conn:
        ensure_link_attempts_table(conn)

        if _attempts_exceeded(conn, chat_id, now):
            log.warning("link_chat: chat %s refused — too many failed /link attempts", chat_id)
            return LinkResult(user_id=None, rate_limited=True)

        if not normalized:
            _record_attempt_failure(conn, chat_id, now)
            return LinkResult(user_id=None)

        row = conn.execute(
            "SELECT user_id, expires_at FROM telegram_link_codes WHERE code = ?",
            (normalized,),
        ).fetchone()
        if row is None:
            _record_attempt_failure(conn, chat_id, now)
            return LinkResult(user_id=None)

        conn.execute("DELETE FROM telegram_link_codes WHERE code = ?", (normalized,))
        expires = _parse_expiry(row["expires_at"])
        if expires is None or expires < now:
            log.info("link code %s rejected: expired at %s", normalized, row["expires_at"])
            _record_attempt_failure(conn, chat_id, now)
            return LinkResult(user_id=None)

        user_id = row["user_id"]

        # Displacement check, BEFORE the writes below overwrite either side:
        # this user_id may already be linked from a DIFFERENT chat (the
        # classic "code leaked / device switch" case) — that old chat is
        # about to lose its link silently, so it's worth a heads-up.
        prior = conn.execute(
            "SELECT chat_id FROM telegram_links WHERE user_id = ?", (user_id,)
        ).fetchone()
        displaced_chat_id = (
            prior["chat_id"] if prior is not None and prior["chat_id"] != chat_id else None
        )

        # The other displacement direction: THIS chat was already linked to
        # a DIFFERENT user — that old user has no other known chat (chat_id
        # is their only channel), so there is nowhere to send them a notice;
        # log it for the audit trail instead.
        prior_user_here = conn.execute(
            "SELECT user_id FROM telegram_links WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if prior_user_here is not None and prior_user_here["user_id"] != user_id:
            log.warning(
                "link_chat: chat %s reassigned from user %s to user %s",
                chat_id,
                prior_user_here["user_id"],
                user_id,
            )

        conn.execute("DELETE FROM telegram_links WHERE user_id = ?", (user_id,))
        conn.execute(
            "INSERT OR REPLACE INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, now.isoformat()),
        )
        _clear_attempts(conn, chat_id)
    log.info("chat %s linked to user %s", chat_id, user_id)
    return LinkResult(user_id=user_id, displaced_chat_id=displaced_chat_id)


def resolve_user(chat_id: int) -> str | None:
    """Return the user_id linked to chat_id, or None for an unbound chat.

    Best-effort: a DB without the telegram_links table yet (pre-migration
    checkout) resolves to None instead of raising — the admin-chat fallback
    in hunter/bot/auth.py keeps the owner functional either way.
    """
    try:
        with get_db(_db_path()) as conn:
            row = conn.execute(
                "SELECT user_id FROM telegram_links WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row["user_id"] if row is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_user(%s) failed: %s", chat_id, e)
        return None


def resolve_chat(user_id: str) -> int | None:
    """Return the chat_id linked to user_id, or None if the user never linked."""
    try:
        with get_db(_db_path()) as conn:
            row = conn.execute(
                "SELECT chat_id FROM telegram_links WHERE user_id = ?", (user_id,)
            ).fetchone()
        return row["chat_id"] if row is not None else None
    except Exception as e:  # noqa: BLE001
        log.warning("resolve_chat(%s) failed: %s", user_id, e)
        return None


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
