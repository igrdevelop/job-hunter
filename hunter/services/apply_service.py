"""Service helpers for apply/hunt orchestration."""

import asyncio
import logging
import subprocess
from pathlib import Path
from typing import Literal

from hunter.models import Job

logger = logging.getLogger(__name__)

# Must match apply_agent.APPLY_MANUAL_EXIT_CODE (JobLeads MANUAL tracker flow)
_APPLY_MANUAL_EXIT_CODE = 44

ApplyOutcome = Literal["ok", "fail", "manual"]


def build_generate_docs_cmd(
    generate_docs_script: Path,
    content_json_path: Path,
    use_full: bool,
    force: bool,
    python_executable: str,
) -> list[str]:
    """Build generate_docs.py command from a concrete content.json path."""
    cmd = [python_executable, str(generate_docs_script), str(content_json_path)]
    if use_full:
        cmd.append("--full")
    if force:
        cmd.append("--force")
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
    if "jobleads.com" in (job.url or "").lower():
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

    if proc.returncode != 0:
        logger.error(
            f"[auto-apply] FAIL {job.company}: {stderr.decode(errors='replace')[-500:]}"
        )
        return "fail"

    if stdout:
        logger.debug(f"[auto-apply] stdout for {job.url}: {stdout.decode(errors='replace')[-300:]}")
    logger.info(f"[auto-apply] OK {job.company} — {job.title}")
    return "ok"
