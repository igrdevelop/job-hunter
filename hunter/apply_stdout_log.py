"""Full stdout/stderr transcript capture for every apply_agent.py subprocess run.

hunter.apply_failures_log records a JSON summary for non-ok outcomes only;
this module keeps the RAW subprocess output (every "[apply_agent] Step ..."
print line, incl. Step 4.5's own react-only-skip decision) for EVERY run,
successes included. That trail is otherwise unobservable in prod: the parent
process logs the child's stdout at DEBUG only (hunter/__main__.py pins both
the console and file handlers to INFO), so a successful run's internal
reasoning is lost the moment the subprocess exits. Added after a 2026-08-14
investigation (GeckoDynamics — a React-only posting that should have been
caught by the pipeline's own react-track guard) hit a dead end for lack of
exactly this data; see the AGENT_LOG entry for that date.

Best-effort throughout: a logging failure must never break an apply run.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# How long a transcript file is kept before opportunistic pruning removes it.
_RETENTION_DAYS = 7
# Guard against an unbounded transcript from a long CLI-mode fallback run
# (M4b spawns ~10-20 sequential `claude -p` calls).
_MAX_TEXT_CHARS = 200_000

_log_dir_override: Path | str | None = None  # test hook — see set_log_dir_for_tests


def set_log_dir_for_tests(path: Path | str | None) -> None:
    """Test-only hook: redirect the transcript directory so tests never touch
    the real logs/apply_stdout/ (mirrors apply_failures_log.set_log_path_for_tests)."""
    global _log_dir_override
    _log_dir_override = path


def _slug(text: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return (s or "unknown")[:60]


def _log_dir() -> Path:
    if _log_dir_override is not None:
        d = Path(_log_dir_override)
    else:
        from hunter.config import PROJECT_DIR

        d = PROJECT_DIR / "logs" / "apply_stdout"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prune_old(d: Path) -> None:
    cutoff = datetime.now(timezone.utc).timestamp() - _RETENTION_DAYS * 86400
    try:
        for f in d.glob("*.log"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
    except OSError as e:
        logger.debug("[apply_stdout_log] prune failed: %s", e)


def save_apply_stdout(
    *,
    url: str,
    company: str = "",
    title: str = "",
    outcome: str,
    exit_code: int | None = None,
    stdout: bytes | None = None,
    stderr: bytes | None = None,
    duration_sec: float | None = None,
) -> None:
    """Write one transcript file for a single apply_agent.py subprocess run.

    Never raises — any failure (disk full, permissions, encoding) is logged
    at DEBUG and swallowed, matching the rest of the apply pipeline's
    best-effort logging contract.
    """
    try:
        now = datetime.now(timezone.utc)
        d = _log_dir()
        name = f"{now.strftime('%Y-%m-%d_%H%M%S')}_{_slug(company or title or url)}.log"
        path = d / name
        out_text = (stdout or b"").decode(errors="replace")[:_MAX_TEXT_CHARS]
        err_text = (stderr or b"").decode(errors="replace")[:_MAX_TEXT_CHARS]
        header = (
            f"ts={now.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"url={url}\n"
            f"company={company}\n"
            f"title={title}\n"
            f"outcome={outcome}\n"
            f"exit_code={exit_code}\n"
            f"duration_sec={round(duration_sec, 1) if duration_sec is not None else ''}\n"
        )
        path.write_text(
            header + "\n--- STDOUT ---\n" + out_text + "\n--- STDERR ---\n" + err_text,
            encoding="utf-8",
        )
        _prune_old(d)
    except Exception as e:  # noqa: BLE001 — logging must never break the apply run
        logger.debug("[apply_stdout_log] failed to write transcript: %s", e)
