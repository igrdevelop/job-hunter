#!/usr/bin/env python3
"""
apply_agent.py — Autonomous apply agent entry point.

Auth priority:
  1. CLI first (Claude Pro subscription) — if `claude` CLI is installed & logged in
  2. API fallback — uses LLM_API_KEY / ANTHROPIC_API_KEY from .env
  Override: --cli forces CLI-only; APPLY_USE_CLI=true in .env does the same.

Doc generation:
  Default (short mode): PDF-only, EN CV only (no PL CV, no .txt files)
  --full flag: all files (DOCX + PDF, PL CV, About_Me .txt files)

Usage:
  python apply_agent.py "https://justjoin.it/job-offer/company-role-city-tech"
  python apply_agent.py "https://nofluffjobs.com/job/some-slug"
  python apply_agent.py --cli "https://..."      # force CLI mode
  python apply_agent.py --full "https://..."     # generate all file types
  python apply_agent.py --force "https://..."    # skip tracker dedup
  python apply_agent.py --paste-file posting.txt            # no URL, use pasted text
  python apply_agent.py --paste-file posting.txt "https://..."  # URL + pasted text
"""

import subprocess
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from hunter.apply_shared import PASTE_NO_URL_PLACEHOLDER
from hunter.apply_api import main_api
from hunter.apply_cli import ApplyError, main_cli
from hunter.config import APPLY_USE_CLI, LLM_API_KEY
from hunter.notify import notify


def _is_cli_available() -> bool:
    """Check if Claude CLI is installed and logged in (Pro subscription)."""
    try:
        r = subprocess.run(
            ["claude", "--version"],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=15,
        )
        if r.returncode != 0:
            return False
        output = (r.stdout + r.stderr).lower()
        if "not logged in" in output or "unauthorized" in output:
            return False
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


def main(
    url: str,
    force_cli: bool = False,
    force: bool = False,
    full: bool = False,
    paste_text: str = "",
    meta_company: str = "",
    meta_title: str = "",
) -> None:
    # Paste flow must use API (CLI can't produce structured content.json reliably).
    if paste_text:
        if not LLM_API_KEY:
            print("[apply_agent] ERROR: --paste-file requires LLM_API_KEY (CLI mode not supported).")
            sys.exit(1)
        main_api(
            url or PASTE_NO_URL_PLACEHOLDER,
            paste_text=paste_text,
            force=force,
            full=full,
            meta_company=meta_company,
            meta_title=meta_title,
        )
        return

    if force_cli or APPLY_USE_CLI:
        main_cli(url, force=force, full=full)
        return

    cli_ok = _is_cli_available()
    if cli_ok:
        print("[apply_agent] Claude CLI detected (Pro subscription) — trying CLI first")
        try:
            main_cli(url, force=force, full=full)
            return
        except (ApplyError, SystemExit) as e:
            if LLM_API_KEY:
                print(f"[apply_agent] CLI failed ({e}), falling back to API mode")
                notify(f"🔄 CLI failed — retrying via API\n🔗 {url}")
            else:
                print(f"[apply_agent] CLI failed and no API key available")
                sys.exit(1)

    if LLM_API_KEY:
        main_api(url, force=force, full=full, meta_company=meta_company, meta_title=meta_title)
    else:
        print("[apply_agent] ERROR: No Claude CLI login and no LLM_API_KEY set. Cannot proceed.")
        sys.exit(1)


def parse_apply_cli_argv(
    argv: list[str],
) -> tuple[str, bool, bool, bool, str, str, str, bool]:
    """Parse argv (including script name).

    Returns: url, force_cli, force, full, company, title, paste_file, notify_start
    """
    args = argv[1:]
    force_cli = "--cli" in args
    force = "--force" in args
    full = "--full" in args
    notify_start = "--notify-start" in args
    company, title, paste_file = "", "", ""
    pos: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--company" and i + 1 < len(args):
            company = args[i + 1]
            i += 2
            continue
        if a == "--title" and i + 1 < len(args):
            title = args[i + 1]
            i += 2
            continue
        if a == "--paste-file" and i + 1 < len(args):
            paste_file = args[i + 1]
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        pos.append(a)
        i += 1
    url = pos[0] if pos else ""
    return url, force_cli, force, full, company, title, paste_file, notify_start


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: python apply_agent.py <job_url> [--cli] [--force] [--full] "
            "[--company NAME] [--title TITLE] [--paste-file PATH] [--notify-start]",
        )
        sys.exit(1)

    url, force_cli, force, full, co, ti, paste_file, notify_start = parse_apply_cli_argv(sys.argv)

    paste_text = ""
    if paste_file:
        try:
            paste_text = Path(paste_file).read_text(encoding="utf-8").strip()
        except Exception as e:
            print(f"[apply_agent] ERROR: failed to read paste file {paste_file}: {e}")
            sys.exit(1)
        if not paste_text:
            print(f"[apply_agent] ERROR: paste file {paste_file} is empty.")
            sys.exit(1)

    if not url and not paste_text:
        print(
            "Usage: python apply_agent.py <job_url> [--cli] [--force] [--full] "
            "[--paste-file PATH] ...",
        )
        sys.exit(1)

    if notify_start:
        label = url if url else "(pasted text)"
        notify(f"🔄 <b>Обрабатываю...</b>\n🔗 {label}\n\nFetching job text & calling LLM…")

    main(url, force_cli=force_cli, force=force, full=full, paste_text=paste_text,
         meta_company=co, meta_title=ti)
