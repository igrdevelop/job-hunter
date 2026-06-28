#!/usr/bin/env python3
"""
apply_agent.py — Thin CLI entry point for the apply pipeline (Phase 4 Step 4.4).

Auth priority:
  1. CLI first (Claude Pro subscription) — if `claude` CLI is installed & logged in
  2. API fallback — uses LLM_API_KEY / ANTHROPIC_API_KEY from .env
  Override: --cli forces CLI-only; APPLY_USE_CLI=true in .env does the same.

Doc generation:
  Default (short mode): PDF-only, EN CV only (no PL CV, no .txt files)
  --full flag: all files (DOCX + PDF, PL CV, About_Me .txt files)

Usage:
  python apply_agent.py "https://justjoin.it/job-offer/company-role-city-tech"
  python apply_agent.py --cli "https://..."      # force CLI mode
  python apply_agent.py --full "https://..."     # generate all file types
  python apply_agent.py --force "https://..."    # skip tracker dedup
  python apply_agent.py --paste-file posting.txt            # no URL, use pasted text
  python apply_agent.py --paste-file posting.txt "https://..."  # URL + pasted text

Architecture (Phase 4):
  hunter/apply_shared.py — shared helpers (constants, Telegram, CL review, etc.)
  hunter/apply_api.py    — API pipeline (fetch → LLM → generate_docs)
  hunter/apply_cli.py    — CLI pipeline (claude -p /apply → post-process)
  apply_agent.py         — this file: arg parsing, dispatch, backward-compat re-exports
"""

import sys
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from hunter.config import APPLY_USE_CLI, LLM_API_KEY

# ── Re-exports for backward compatibility with tests and external callers ──────
# These symbols moved to hunter/apply_shared.py in Phase 4 Step 4.1.
from hunter.apply_shared import (  # noqa: F401
    APPLY_MANUAL_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    ApplyError,
    _already_processed,
    _body_banlist_hits,
    _cover_letter_review,
    _cover_letter_review_loop,
    _opener_banlist_hits,
    compute_output_folder,
    notify,
    send_telegram_documents,
    validate_content,
)

# ── Pipeline entry points ──────────────────────────────────────────────────────
from hunter.apply_api import main_api  # noqa: F401
from hunter.apply_cli import _is_cli_available, main_cli  # noqa: F401


# ── Main dispatcher ────────────────────────────────────────────────────────────

def main(
    url: str,
    force_cli: bool = False,
    force: bool = False,
    full: bool = False,
    paste_text: str = "",
    jobleads_company: str = "",
    jobleads_title: str = "",
) -> None:
    """Dispatch to CLI or API pipeline based on availability and flags."""
    if force_cli or APPLY_USE_CLI:
        folder = main_cli(url, skip_dedup=force, full_mode=full, paste_text=paste_text)
        _maybe_run_shadow(folder, full=full)
        return

    cli_ok = _is_cli_available()
    if cli_ok:
        print("[apply_agent] Claude CLI detected (Pro subscription) — trying CLI first")
        try:
            folder = main_cli(url, skip_dedup=force, full_mode=full, paste_text=paste_text)
            _maybe_run_shadow(folder, full=full)
            return
        except (ApplyError, SystemExit) as e:
            if LLM_API_KEY:
                print(f"[apply_agent] CLI failed ({e}), falling back to API mode")
                notify(f"🔄 CLI failed — retrying via API\n🔗 {url}")
            else:
                print("[apply_agent] CLI failed and no API key available")
                sys.exit(1)

    if LLM_API_KEY:
        folder = main_api(
            url or PASTE_NO_URL_PLACEHOLDER,
            paste_text=paste_text,
            skip_dedup=force,
            full_mode=full,
            jobleads_company=jobleads_company,
            jobleads_title=jobleads_title,
        )
        _maybe_run_shadow(folder, full=full)
    else:
        print("[apply_agent] ERROR: No Claude CLI login and no LLM_API_KEY set. Cannot proceed.")
        sys.exit(1)


def _maybe_run_shadow(folder, full: bool) -> None:
    """Fire-and-forget the dual-apply shadow (delegated to hunter.dual_apply, which
    runs it detached so it can't affect this process's exit code/timeout). No-op
    when the primary was skipped (folder is None). Best-effort."""
    if not folder:
        return
    try:
        from hunter.dual_apply import launch_detached
        launch_detached(folder, full_mode=full)
    except Exception as e:
        print(f"[apply_agent] dual-apply shadow launch skipped: {e}")


# ── CLI argument parser ────────────────────────────────────────────────────────

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


# ── __main__ block ─────────────────────────────────────────────────────────────

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
        notify(f"🔄 <b>Processing...</b>\n🔗 {label}\n\nFetching job text & calling LLM…")

    main(url, force_cli=force_cli, force=force, full=full, paste_text=paste_text,
         jobleads_company=co, jobleads_title=ti)
