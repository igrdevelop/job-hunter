"""Post-start Claude CLI canary (docs/APPLY_FAILURE_QUEUES_PLAN.md M1).

Prod runs on the paid API; the Claude CLI pipeline (`hunter/apply_cli.py`) is
exercised only when the API account is down. From 2026-09-10 to 2026-09-21 its
argv was broken (the prompt sat after the variadic `--disallowedTools` and was
parsed as tool rules), and nothing noticed. The failure only happened during
API outages, and there `apply_agent` reports it as another outage (exit 46),
which is kept out of `logs/apply_failures.jsonl` by design. The M0 measurement
confirmed it: the incident left zero lines in the failure log.

So the CLI path is checked directly, once per bot start: one trivial
`claude -p` built by `apply_cli._cli_argv`, the same builder a real apply uses.
It gets the same tool flags in the same order, the same cwd and the same env.
A pass is logged; a failure sends one Telegram alert. Nothing is blocked (the
plan's M3 `BLOCKED` queue was closed by the M0 verdict).

Cost: one call on the flat-rate CLI subscription per restart, no API spend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from html import escape

logger = logging.getLogger(__name__)

CANARY_PROMPT = "Reply with exactly the word OK and nothing else. Do not use any tools."
CANARY_TIMEOUT_SEC = 180
RETRY_DELAY_SEC = 60
_DETAIL_CHARS = 600

# The 2026-09-10 signature: the CLI rejecting parts of its own argv. Checked on
# stderr AND stdout because the CLI prints these as warnings before it fails,
# and a future CLI version might still answer after printing them.
_ARGV_REJECTED_RE = re.compile(
    r"matches no known tool|unknown option|unrecognized (?:option|argument)|error: option",
    re.IGNORECASE,
)

# The whole reply must be the word OK. Case and surrounding punctuation are
# ignored ("ok", "OK.", "`OK`"): the canary checks that the CLI invocation
# works, not the model's obedience, and a false alarm on every restart would
# teach the owner to ignore the real one. A qualified reply ("OK, but I
# cannot continue") or one that merely contains the letters ("BOOK") fails.
_OK_REPLY_RE = re.compile(r"[\W_]*ok[\W_]*", re.IGNORECASE)

# Keeps a reference so the background task isn't garbage-collected mid-run.
_task: asyncio.Task | None = None


@dataclass(frozen=True)
class CanaryResult:
    ok: bool
    reason: str
    detail: str = ""
    # Deterministic failures (bad argv, missing binary) are not retried: the
    # second run would produce the same answer 60 s later.
    retryable: bool = True


def _head(text: str) -> str:
    return (text or "").strip()[:_DETAIL_CHARS]


def run_canary(timeout: int = CANARY_TIMEOUT_SEC) -> CanaryResult:
    """Run one probe synchronously. Never raises."""
    from hunter.apply_cli import _cli_argv
    from hunter.config import PROJECT_DIR

    cmd = _cli_argv(CANARY_PROMPT)
    try:
        proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
            cmd,
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=os.environ,
        )
    except FileNotFoundError:
        return CanaryResult(False, "claude binary not found", retryable=False)
    except subprocess.TimeoutExpired:
        return CanaryResult(False, f"timed out after {timeout}s")
    except OSError as e:
        return CanaryResult(False, f"could not start claude: {e}")

    out, err = proc.stdout or "", proc.stderr or ""
    if _ARGV_REJECTED_RE.search(err) or _ARGV_REJECTED_RE.search(out):
        return CanaryResult(
            False,
            "the CLI rejected the apply pipeline's own arguments",
            _head(err or out),
            retryable=False,
        )
    if proc.returncode != 0:
        return CanaryResult(False, f"exit code {proc.returncode}", _head(err or out))
    if not _OK_REPLY_RE.fullmatch(out.strip()):
        return CanaryResult(False, "unexpected reply", _head(out or err))
    return CanaryResult(True, "ok")


def format_alert(result: CanaryResult) -> str:
    lines = [
        "🚨 <b>Claude CLI canary failed</b>",
        f"Reason: {escape(result.reason)}",
        "The CLI is the fallback when the paid API is down. Until this is "
        "fixed, an API outage stops generation completely.",
    ]
    if result.detail:
        lines.append(f"<pre>{escape(result.detail)}</pre>")
    return "\n".join(lines)


async def run_startup_canary(app, *, retry_delay: int = RETRY_DELAY_SEC) -> CanaryResult | None:
    """Probe the CLI once (retrying a transient failure once) and alert on failure.

    Returns None when skipped (disabled, or no CLI login configured, i.e. the
    fallback isn't meant to exist on this host).
    """
    from hunter.best_effort import best_effort
    from hunter.config import CLI_CANARY_ENABLED

    if not CLI_CANARY_ENABLED:
        return None

    with best_effort("apply.cli_canary"):
        from llm_client import cli_credentials_present

        if not await asyncio.to_thread(cli_credentials_present):
            logger.info("[cli_canary] no CLI login configured — skipped")
            return None

        result = await asyncio.to_thread(run_canary)
        if not result.ok and result.retryable:
            logger.warning("[cli_canary] failed (%s) — retrying in %ss", result.reason, retry_delay)
            await asyncio.sleep(retry_delay)
            result = await asyncio.to_thread(run_canary)

        if result.ok:
            logger.info("[cli_canary] CLI apply invocation OK")
        else:
            logger.error("[cli_canary] FAILED: %s | %s", result.reason, result.detail)
            from hunter.bot.notifications import send_text

            await send_text(app, format_alert(result))
        return result
    return None


def start(app) -> None:
    """Schedule the canary in the background from `_post_init`.

    A plain asyncio task, not `app.create_task()`: PTB's `Application.stop()`
    awaits every tracked task, and there is no reason to hold shutdown for a
    probe (same reasoning as the apply worker in `telegram_bot._post_init`).
    """
    global _task
    _task = asyncio.create_task(run_startup_canary(app), name="cli_canary")
