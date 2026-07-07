"""Service helpers for apply/hunt orchestration."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Literal, Optional

from hunter.models import Job

logger = logging.getLogger(__name__)

# Must match apply_agent.APPLY_MANUAL_EXIT_CODE (JobLeads MANUAL tracker flow)
_APPLY_MANUAL_EXIT_CODE = 44
# Must match apply_shared.APPLY_RATE_LIMITED_EXIT_CODE (transient 429 during fetch)
_APPLY_RATE_LIMITED_EXIT_CODE = 45

ApplyOutcome = Literal["ok", "fail", "manual", "rate_limited"]

# Second element: human-readable error snippet for Telegram (empty string on success).
ApplyResult = tuple[ApplyOutcome, str]


def build_generate_docs_cmd(
    generate_docs_script: Path,
    content_json_path: Path,
    use_full: bool,
    force: bool,
    python_executable: str,
    no_tracker: bool = False,
) -> list[str]:
    """Build generate_docs.py command from a concrete content.json path.

    no_tracker=True passes --no-tracker so the render skips the tracker write
    (used by the dual-apply shadow run).
    """
    cmd = [python_executable, str(generate_docs_script), str(content_json_path)]
    if use_full:
        cmd.append("--full")
    if force:
        cmd.append("--force")
    if no_tracker:
        cmd.append("--no-tracker")
    return cmd


async def run_apply_agent_subprocess(
    job: Job,
    timeout_sec: int,
    apply_agent_path: Path,
    python_executable: str,
) -> ApplyOutcome:
    """Run apply_agent.py as async subprocess.

    Returns ``ok`` on exit 0, ``manual`` on JobLeads MANUAL flow (exit 44), ``fail`` otherwise.
    """
    cmd = [python_executable, str(apply_agent_path), job.url]
    # --company/--title used to be JobLeads-only (its detail pages are Cloudflare-
    # blocked, so the MANUAL tracker row needs a listing-derived title/company).
    # Passed for every auto-hunt job now (docs/DOOMED_GATE_PASTE_PLAN.md): without
    # it, the doomed gate's title-based checks (title_exclude_pattern/
    # off_domain_title) never see the REAL hunt-listing title and fall back to
    # guessing one from the raw fetched text — noisy and unnecessary when the
    # Job object already has a perfectly good title. Only a genuine manual paste
    # (no Job object, just a typed URL/text) still needs the guess.
    if job.title or job.company:
        safe_title = (job.title or "Unknown").replace("\r\n", " ").replace("\n", " ").strip()[:500]
        cmd.extend(["--company", job.company or "Unknown", "--title", safe_title])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"[auto-apply] failed to start subprocess for {job.url}: {e}")
        return "fail"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.error(f"[auto-apply] TIMEOUT ({timeout_sec}s) for {job.url}")
        return "fail"

    if proc.returncode == _APPLY_MANUAL_EXIT_CODE:
        logger.info(f"[auto-apply] MANUAL pending (JobLeads) {job.company} — {job.title}")
        return "manual"

    if proc.returncode == _APPLY_RATE_LIMITED_EXIT_CODE:
        logger.warning(f"[auto-apply] RATE-LIMITED (429) {job.company} — {job.title}")
        return "rate_limited"

    if proc.returncode != 0:
        logger.error(
            f"[auto-apply] FAIL {job.company}: {stderr.decode(errors='replace')[-500:]}"
        )
        return "fail"

    if stdout:
        logger.debug(f"[auto-apply] stdout for {job.url}: {stdout.decode(errors='replace')[-300:]}")
    logger.info(f"[auto-apply] OK {job.company} — {job.title}")
    return "ok"


async def run_apply_agent_for_url(
    url: str,
    timeout_sec: int,
    apply_agent_path: Path,
    python_executable: str,
    force: bool = False,
    paste_file: Optional[str] = None,
) -> ApplyResult:
    """URL-based variant of run_apply_agent_subprocess for manual Telegram triggers.

    Unlike the Job-based variant, accepts a plain URL and optional flags for
    force-apply and paste-file flow (no Job object required).

    Returns (outcome, error_detail):
      outcome    — "ok" | "fail" | "manual"
      error_detail — non-empty string on failure (stderr snippet / timeout reason)
    """
    label = url or "(pasted text)"
    cmd = [python_executable, str(apply_agent_path)]
    if url:
        cmd.append(url)
    if force:
        cmd.append("--force")
    if paste_file:
        cmd.extend(["--paste-file", paste_file])
    # Signal apply_agent.py to send an early Telegram notification confirming start
    cmd.append("--notify-start")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (OSError, subprocess.SubprocessError) as e:
        logger.error(f"[apply_agent] failed to start subprocess for {label}: {e}")
        return "fail", f"Failed to start process: {e}"

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=timeout_sec,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        logger.error(f"[apply_agent] TIMEOUT ({timeout_sec}s) for {label}")
        return "fail", f"Timed out after {timeout_sec}s"

    if proc.returncode == _APPLY_MANUAL_EXIT_CODE:
        logger.info(f"[apply_agent] MANUAL pending (JobLeads) {label}")
        return "manual", ""

    stderr_text = stderr.decode(errors="replace") if stderr else ""
    if proc.returncode != 0:
        snippet = stderr_text[-600:].strip() if stderr_text else "(no stderr)"
        logger.error(f"[apply_agent] FAIL for {label}: {snippet}")
        return "fail", snippet

    if stdout:
        logger.debug(f"[apply_agent] stdout for {label}: {stdout.decode(errors='replace')[-300:]}")
    logger.info(f"[apply_agent] OK {label}")
    return "ok", ""
