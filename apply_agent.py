#!/usr/bin/env python3
"""
apply_agent.py — Autonomous apply agent (CLI entry point).

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

Architecture note (Phase 4):
  Business logic lives in hunter/apply_shared.py + hunter/apply_api.py + hunter/apply_cli.py.
  This file is the thin CLI entry point and backward-compat re-export shim.
"""

import json
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from hunter.config import (
    APPLY_USE_CLI,
    APPLICATIONS_DIR,
    CLI_MAX_RETRIES,
    CLI_RETRY_DELAY,
    GENERATE_DOCS_PATH,
    LLM_API_KEY,
    PROJECT_DIR,
)
from hunter.services.apply_service import build_generate_docs_cmd

# ── Re-exports for backward compatibility with tests and external callers ──────
# These symbols moved to hunter/apply_shared.py in Phase 4 Step 4.1.
from hunter.apply_shared import (  # noqa: E402
    APPLY_MANUAL_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    _REACT_SKIP_FORCE_HINT,
    ApplyError,
    _already_processed,
    _body_banlist_hits,
    _cover_letter_review_loop,
    _opener_banlist_hits,
    compute_output_folder,
    notify,
    send_telegram_documents,
    validate_content,
)

# ── Re-export API pipeline ─────────────────────────────────────────────────────
from hunter.apply_api import main_api  # noqa: E402

# ── Kept here until Step 4.2 moves them to hunter/apply_cli.py ────────────────


def _get_existing_folders() -> set[str]:
    """Return relative paths of all known application folders.

    New structure:  Applications/{date}/{Company}  → stored as "{date}/{Company}"
    Legacy flat:    Applications/{Company}_{date}   → stored as "{Company}_{date}"
    """
    if not APPLICATIONS_DIR.exists():
        return set()
    result: set[str] = set()
    for item in APPLICATIONS_DIR.iterdir():
        if not item.is_dir():
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}$", item.name):
            for sub in item.iterdir():
                if sub.is_dir():
                    result.add(f"{item.name}/{sub.name}")
        else:
            result.add(item.name)
    return result


def _find_new_folder(before: set[str], timeout: int = 300) -> str | None:
    """Detect a newly created application folder.

    Searches today's date subfolder first (new structure), then falls back to
    scanning the flat Applications/ directory (legacy / CLI created outside date dir).
    """
    today = date.today().strftime("%Y-%m-%d")
    date_dir = APPLICATIONS_DIR / today
    run_start = time.time()
    deadline = run_start + max(timeout, 0)
    while True:
        if date_dir.exists():
            for folder in date_dir.iterdir():
                if not folder.is_dir():
                    continue
                rel = f"{today}/{folder.name}"
                if rel not in before:
                    return rel
                if folder.stat().st_mtime >= run_start - 5:
                    return rel
        if APPLICATIONS_DIR.exists():
            for folder in APPLICATIONS_DIR.iterdir():
                if not folder.is_dir():
                    continue
                if re.match(r"^\d{4}-\d{2}-\d{2}$", folder.name):
                    continue
                if folder.name not in before:
                    return folder.name
                if folder.stat().st_mtime >= run_start - 5:
                    return folder.name
        if time.time() >= deadline:
            break
        time.sleep(5)
    return None


def main_cli(url: str, *, skip_dedup: bool = False, full_mode: bool = False) -> None:
    print(f"\n[apply_agent] CLI mode | URL: {url}\n")

    if _already_processed(url, skip_dedup=skip_dedup):
        try:
            from hunter.tracker import lookup_url
            rows = lookup_url(url)
            detail = ""
            if rows:
                r = rows[0]
                detail = (
                    f"\n\nRow {r['row']}: <b>{r['company']}</b> — {r['title']}"
                    f"\nATS: {r['ats']}  Sent: {r['sent']}"
                    + (f"\nFolder: <code>{r['folder']}</code>" if r.get("folder") else "")
                )
        except Exception:
            detail = ""
        notify(f"ℹ️ <b>Already in tracker — skipped</b>\n🔗 {url}{detail}")
        print(f"[apply_agent] SKIP — already in tracker: {url}")
        return

    folders_before = _get_existing_folders()

    apply_input = url
    job_text: str | None = None
    try:
        from hunter.sources import fetch_job_text
        job_text = fetch_job_text(url)
        if job_text and len(job_text) > 100:
            apply_input = f"URL: {url}\n\n{job_text}"
            print(f"[apply_agent] Pre-fetched {len(job_text)} chars via JSON API")
    except Exception as e:
        print(f"[apply_agent] Pre-fetch failed ({e}), passing raw URL to Claude")

    if job_text:
        from hunter.expired_check import is_job_expired
        if is_job_expired(job_text):
            notify(f"⏭ <b>Expired — skipped</b>\n🔗 {url}")
            print(f"[apply_agent] EXPIRED — offer no longer active: {url}")
            try:
                from hunter.tracker import add_expired
                add_expired(url)
            except Exception as e:
                print(f"[apply_agent] Warning: could not write EXPIRED to tracker: {e}")
            return

    cmd = ["claude", "-p", "--dangerously-skip-permissions", f"/apply {apply_input}"]
    print(f"[apply_agent] Running claude CLI...\n")

    result = None
    new_folder_timeout = None

    for attempt in range(1, CLI_MAX_RETRIES + 1):
        try:
            result = subprocess.run(
                cmd,
                cwd=str(PROJECT_DIR),
                capture_output=True,
                stdin=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
            )
        except subprocess.TimeoutExpired:
            new_folder_on_timeout = _find_new_folder(folders_before, timeout=0)
            if new_folder_on_timeout:
                print(f"\n[apply_agent] Claude timed out but folder created: {new_folder_on_timeout}")
                result = None
                new_folder_timeout = new_folder_on_timeout
                break
            else:
                notify(f"⏱ <b>apply_agent timeout (10 min)</b>\nURL: {url}")
                print(f"\n[apply_agent] Timeout — no folder created.")
                raise ApplyError("CLI timeout — no folder created")

        if result.returncode == 0:
            break

        output = (result.stderr or result.stdout or "")
        is_overloaded = "overloaded" in output.lower() or "529" in output

        if is_overloaded and attempt < CLI_MAX_RETRIES:
            wait = CLI_RETRY_DELAY * attempt
            print(f"[apply_agent] Claude overloaded (529), retry {attempt}/{CLI_MAX_RETRIES} in {wait}s...")
            notify(f"⚠️ Claude overloaded (529), retry {attempt}/{CLI_MAX_RETRIES} in {wait}s...")
            time.sleep(wait)
            continue

        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[apply_agent] STDERR:", result.stderr, file=sys.stderr)
        error_detail = (result.stderr or result.stdout or "no output")[:800]
        notify(
            f"❌ <b>apply_agent CLI failed</b>\n"
            f"URL: {url}\n"
            f"Exit code: {result.returncode}"
            + (f" (attempt {attempt}/{CLI_MAX_RETRIES})" if attempt > 1 else "")
            + f"\n\n<pre>{error_detail}</pre>"
        )
        print(f"\n[apply_agent] claude exited with code {result.returncode}")
        raise ApplyError(f"CLI exited with code {result.returncode}")

    if result is not None and result.returncode == 0:
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[apply_agent] STDERR:", result.stderr, file=sys.stderr)

    new_folder = new_folder_timeout or _find_new_folder(folders_before, timeout=30)

    if new_folder:
        folder_path = APPLICATIONS_DIR / new_folder

        if job_text:
            try:
                job_posting_path = folder_path / "job_posting.txt"
                job_posting_path.write_text(f"URL: {url}\n\n{job_text}", encoding="utf-8")
                print(f"[apply_agent] Saved job posting -> {job_posting_path.name}")
            except Exception as e:
                print(f"[apply_agent] Warning: could not save job_posting.txt: {e}")

        content_json_path = folder_path / "content.json"
        if content_json_path.exists():
            try:
                _cli_content = json.loads(content_json_path.read_text(encoding="utf-8"))

                _cli_stack = (_cli_content.get("stack") or "").lower()
                if "react" in _cli_stack and "angular" not in _cli_stack and not skip_dedup:
                    notify(
                        f"⏭ <b>Skipped — React-only stack</b>\n"
                        f"🔗 {url}\n"
                        f"Stack: {_cli_content.get('stack', '?')}"
                        f"{_REACT_SKIP_FORCE_HINT}"
                    )
                    print(f"[apply_agent] SKIP — React-only stack: {_cli_content.get('stack')}")
                    try:
                        from hunter.tracker import add_react_skipped
                        add_react_skipped(_cli_content, url)
                    except Exception as e:
                        print(f"[apply_agent] Warning: could not write React-skip to tracker: {e}")
                    return

                print("[apply_agent] Cover letter review (CLI mode)...")
                _cli_content_reviewed = _cover_letter_review_loop(_cli_content)
                if _cli_content_reviewed.get("cover_letter_en") != _cli_content.get("cover_letter_en"):
                    content_json_path.write_text(
                        json.dumps(_cli_content_reviewed, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    print("[apply_agent] Regenerating docs with rewritten cover letter...")
                    gen_cmd = build_generate_docs_cmd(
                        generate_docs_script=GENERATE_DOCS_PATH,
                        content_json_path=content_json_path,
                        use_full=full_mode,
                        force=skip_dedup,
                        python_executable=sys.executable,
                    )
                    subprocess.run(gen_cmd, cwd=str(PROJECT_DIR), check=False)

            except Exception as e:
                print(f"[apply_agent] CLI post-processing error: {e}")

        created_files = list(folder_path.glob("*.docx")) + list(folder_path.glob("*.pdf"))
        if created_files:
            file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
            notify(
                f"✅ <b>Docs ready!</b>\n\n"
                f"📁 <code>Applications/{new_folder}/</code>\n\n"
                f"{file_names}\n\n"
                f"Via: CLI (Pro subscription)\n"
                f"Review and send when ready."
            )
            send_telegram_documents(created_files)
            print(f"\n[apply_agent] Done! Folder: Applications/{new_folder}/ ({len(created_files)} files)")
        else:
            notify(
                f"⚠️ <b>Folder created but no docs found!</b>\n"
                f"📁 <code>Applications/{new_folder}/</code>\n"
                f"Check the folder for partial output."
            )
            print(f"\n[apply_agent] WARNING: Folder created but no .docx/.pdf files found.")
            raise ApplyError("Folder created but no docs found")
    else:
        stdout_preview = (result.stdout or "").strip()[:600] if result else ""
        notify(
            f"❌ <b>CLI exited 0 but no folder created</b>\n"
            f"🔗 {url}\n\n"
            + (f"Claude output:\n<pre>{stdout_preview}</pre>" if stdout_preview else "No CLI output captured.")
        )
        print(f"\n[apply_agent] FAIL: claude exited 0 but no new folder was created.")
        raise ApplyError("No output folder created")


# ── Entry point ───────────────────────────────────────────────────────────────

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
    jobleads_company: str = "",
    jobleads_title: str = "",
) -> None:
    # Pasted text skips fetch, so CLI mode can't help — force API.
    if paste_text:
        if not LLM_API_KEY:
            print("[apply_agent] ERROR: --paste-file requires LLM_API_KEY (CLI mode not supported).")
            sys.exit(1)
        main_api(
            url or PASTE_NO_URL_PLACEHOLDER,
            paste_text=paste_text,
            skip_dedup=force,
            full_mode=full,
            jobleads_company=jobleads_company,
            jobleads_title=jobleads_title,
        )
        return

    if force_cli or APPLY_USE_CLI:
        main_cli(url, skip_dedup=force, full_mode=full)
        return

    cli_ok = _is_cli_available()
    if cli_ok:
        print("[apply_agent] Claude CLI detected (Pro subscription) — trying CLI first")
        try:
            main_cli(url, skip_dedup=force, full_mode=full)
            return
        except (ApplyError, SystemExit) as e:
            if LLM_API_KEY:
                print(f"[apply_agent] CLI failed ({e}), falling back to API mode")
                notify(f"🔄 CLI failed — retrying via API\n🔗 {url}")
            else:
                print(f"[apply_agent] CLI failed and no API key available")
                sys.exit(1)

    if LLM_API_KEY:
        main_api(
            url,
            skip_dedup=force,
            full_mode=full,
            jobleads_company=jobleads_company,
            jobleads_title=jobleads_title,
        )
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
        notify(f"🔄 <b>Processing...</b>\n🔗 {label}\n\nFetching job text & calling LLM…")

    main(url, force_cli=force_cli, force=force, full=full, paste_text=paste_text,
         jobleads_company=co, jobleads_title=ti)
