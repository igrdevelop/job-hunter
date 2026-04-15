"""Service helpers for apply/hunt orchestration."""

import asyncio
import logging
from pathlib import Path

from hunter.models import Job

logger = logging.getLogger(__name__)


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
) -> bool:
    """Run apply_agent.py as async subprocess, return True on success."""
    try:
        proc = await asyncio.create_subprocess_exec(
            python_executable,
            str(apply_agent_path),
            job.url,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=timeout_sec,
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            logger.error(f"[auto-apply] TIMEOUT ({timeout_sec}s) for {job.url}")
            return False

        if proc.returncode != 0:
            logger.error(
                f"[auto-apply] FAIL {job.company}: {stderr.decode(errors='replace')[-500:]}"
            )
            return False

        if stdout:
            logger.debug(f"[auto-apply] stdout for {job.url}: {stdout.decode(errors='replace')[-300:]}")
        logger.info(f"[auto-apply] OK {job.company} — {job.title}")
        return True
    except Exception as e:
        logger.error(f"[auto-apply] exception for {job.url}: {e}")
        return False
