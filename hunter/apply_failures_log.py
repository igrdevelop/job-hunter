"""Structured fail-audit log for the apply pipeline (docs/HUNT_APPLY_SPLIT_PLAN.md M4).

Every non-ok, non-manual, non-llm_outage apply outcome (fail, cli_timeout,
rate_limited) appends one JSON line to `logs/apply_failures.jsonl` — a
grep-able, `/fails`-readable trail independent of the free-text
`logs/hunter_errors.log`. `llm_outage` is excluded: it's a global account
state recorded once via the M2 pause (hunter.llm_outage), not a per-vacancy
failure worth an audit-log row.

Best-effort throughout: a logging failure must never break an apply run.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

logger = logging.getLogger(__name__)

_FAILURE_LOGGER_NAME = "hunter.apply_failures_jsonl"
_MAX_ERROR_CHARS = 500

# Outcomes that are genuinely a per-vacancy failure worth auditing. Kept as a
# module constant (not inlined at each call site) so a new outcome added to
# ApplyOutcome later is an explicit, reviewable decision here.
LOGGED_OUTCOMES = frozenset({"fail", "cli_timeout", "cli_no_output", "rate_limited"})

_log_path_override: Path | str | None = None  # test hook — see set_log_path_for_tests


def _desired_log_path() -> Path:
    if _log_path_override is not None:
        return Path(_log_path_override)
    from hunter.config import PROJECT_DIR

    log_dir = PROJECT_DIR / "logs"
    log_dir.mkdir(exist_ok=True)
    return log_dir / "apply_failures.jsonl"


def _clear_failure_handlers() -> None:
    log_obj = logging.getLogger(_FAILURE_LOGGER_NAME)
    for h in list(log_obj.handlers):
        log_obj.removeHandler(h)
        h.close()


def _get_failure_logger() -> logging.Logger:
    """Lazily create (once per process) the JSONL file logger.

    `propagate = False` keeps these lines out of hunter_errors.log — they'd
    otherwise be duplicated (once as a plain-text log line, once as JSON).

    Handlers are reused ONLY when they already point at the current desired
    path. A bare `if log_obj.handlers: return` is wrong: pytest (or a prior
    test) can leave a StreamHandler / closed FileHandler on this logger, and
    we'd then .info() into the void while the test's tmp JSONL never appears.
    """
    log_obj = logging.getLogger(_FAILURE_LOGGER_NAME)
    path = _desired_log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    desired = path.resolve()

    for h in list(log_obj.handlers):
        if (
            isinstance(h, RotatingFileHandler)
            and Path(getattr(h, "baseFilename", "")).resolve() == desired
        ):
            log_obj.setLevel(logging.INFO)
            log_obj.propagate = False
            return log_obj

    _clear_failure_handlers()

    handler = RotatingFileHandler(
        path,
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    log_obj.addHandler(handler)
    log_obj.setLevel(logging.INFO)
    log_obj.propagate = False
    return log_obj


def set_log_path_for_tests(path) -> None:
    """Test-only hook: redirect the JSONL file + drop any cached logger/handlers
    so the next log_apply_failure() call reopens at the new path."""
    global _log_path_override
    _log_path_override = path
    _clear_failure_handlers()


def log_apply_failure(
    *,
    url: str,
    outcome: str,
    company: str = "",
    title: str = "",
    exit_code: int | None = None,
    error: str = "",
    duration_sec: float | None = None,
    cli_mode: bool = False,
) -> None:
    """Append one JSON line for a non-ok apply outcome.

    No-ops (silently) for outcomes not in LOGGED_OUTCOMES — callers may pass
    any outcome string without checking membership themselves.
    """
    if outcome not in LOGGED_OUTCOMES:
        return
    try:
        record = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": url,
            "company": company,
            "title": title,
            "outcome": outcome,
            "exit_code": exit_code,
            "error": (error or "")[:_MAX_ERROR_CHARS],
            "duration_sec": round(duration_sec, 1) if duration_sec is not None else None,
            "cli_mode": bool(cli_mode),
        }
        _get_failure_logger().info(json.dumps(record, ensure_ascii=False))
    except Exception as e:  # noqa: BLE001 — logging must never break the apply run
        logger.debug("[apply_failures_log] failed to write record: %s", e)


def read_last_failures(n: int = 5) -> list[dict]:
    """Return the last `n` records from the CURRENT log file, oldest first.

    Read-only, best-effort: a missing/unreadable file (or a corrupt trailing
    line from a mid-write crash) yields fewer/no records rather than raising
    — a Telegram command must never 500 on a broken log line.
    """
    path = _desired_log_path()
    try:
        with open(path, encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return []

    records: list[dict] = []
    for line in lines[-n:]:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
