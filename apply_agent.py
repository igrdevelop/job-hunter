#!/usr/bin/env python3
"""
apply_agent.py — Autonomous apply agent.

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
"""

import json
import os
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

import requests

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(__file__).parent
ENV_PATH = PROJECT_DIR / ".env"
APPLICATIONS_DIR = PROJECT_DIR / "Applications"
PROMPTS_DIR = PROJECT_DIR / "prompts"
GENERATE_DOCS_SCRIPT = PROJECT_DIR / "generate_docs.py"

# Load .env
if ENV_PATH.exists():
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# LLM config
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "anthropic")
LLM_MODEL = os.environ.get("LLM_MODEL", "claude-3-5-haiku-20241022")
LLM_API_KEY = os.environ.get("LLM_API_KEY", "") or os.environ.get("ANTHROPIC_API_KEY", "")
APPLY_USE_CLI = os.environ.get("APPLY_USE_CLI", "false").lower() in ("true", "1", "yes")
CLI_MAX_RETRIES = int(os.environ.get("CLI_MAX_RETRIES", "3"))
CLI_RETRY_DELAY = int(os.environ.get("CLI_RETRY_DELAY", "30"))

GENERATE_PL_RESUME = os.environ.get("GENERATE_PL_RESUME", "false").lower() in ("true", "1", "yes")

REQUIRED_JSON_KEYS = [
    "company_name", "stack", "lang", "job_title",
    "resume_en", "cover_letter_en", "cover_letter_pl",
    "about_me_en", "about_me_pl",
]
if GENERATE_PL_RESUME:
    REQUIRED_JSON_KEYS.append("resume_pl")

_SKIP_DEDUP = False
_FULL_MODE = False


# ── Tracker dedup check (avoid wasting LLM tokens) ──────────────────────────

def _already_processed(url: str) -> bool:
    """Check tracker.xlsx before calling LLM.

    Returns True only if a SUCCESSFUL entry exists (ATS score is a real value,
    not FAIL/SKIP). This way retries of FAIL jobs are not blocked.
    Skipped entirely when --force flag is used.
    """
    if _SKIP_DEDUP:
        return False
    try:
        sys.path.insert(0, str(PROJECT_DIR))
        from hunter.tracker import has_successful_entry
        return has_successful_entry(url)
    except Exception:
        return False


# ── Telegram helper ───────────────────────────────────────────────────────────

def notify(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply_agent] Telegram error: {e}")


# ── Output folder logic ──────────────────────────────────────────────────────

def compute_output_folder(company_name: str) -> Path:
    """Compute Applications/{Company}_{date} with _2, _3 suffixes if needed."""
    today = date.today().strftime("%Y-%m-%d")
    base = APPLICATIONS_DIR / f"{company_name}_{today}"
    if not base.exists():
        return base
    suffix = 2
    while True:
        candidate = APPLICATIONS_DIR / f"{company_name}_{today}_{suffix}"
        if not candidate.exists():
            return candidate
        suffix += 1


def validate_content(data: dict) -> list[str]:
    """Return list of missing/invalid fields."""
    errors = []
    for key in REQUIRED_JSON_KEYS:
        if key not in data or data[key] is None:
            errors.append(f"Missing field: {key}")

    resume = data.get("resume_en")
    if isinstance(resume, dict):
        for sub in ("summary", "skills", "experience", "education"):
            if sub not in resume:
                errors.append(f"resume_en missing: {sub}")
        if isinstance(resume.get("experience"), list) and len(resume["experience"]) < 3:
            errors.append(f"resume_en.experience has only {len(resume['experience'])} jobs (expected 6)")
    else:
        errors.append("resume_en is not a dict")

    return errors


# ══════════════════════════════════════════════════════════════════════════════
# API MODE — fetch job → LLM → content.json → generate_docs
# ══════════════════════════════════════════════════════════════════════════════

def main_api(url: str) -> None:
    print(f"\n[apply_agent] API mode | URL: {url}\n")

    if _already_processed(url):
        print(f"[apply_agent] SKIP — already in tracker: {url}")
        return

    notify(
        f"⏳ <b>Generating docs (API)...</b>\n"
        f"URL: {url}\n"
        f"Model: {LLM_MODEL}"
    )

    # Step 1 — Fetch job text
    print("[apply_agent] Step 1: Fetching job posting...")
    try:
        from job_fetch import fetch_job_text
        job_text = fetch_job_text(url)
        print(f"[apply_agent] Fetched {len(job_text)} chars of job text")
    except Exception as e:
        notify(f"❌ <b>Failed to fetch job posting</b>\nURL: {url}\n\n<pre>{str(e)[:400]}</pre>")
        print(f"[apply_agent] FETCH ERROR: {e}")
        sys.exit(1)

    # Step 2 — Read system prompt
    prompt_path = PROMPTS_DIR / "system_prompt.md"
    if not prompt_path.exists():
        print(f"[apply_agent] ERROR: {prompt_path} not found")
        sys.exit(1)
    system_prompt = prompt_path.read_text(encoding="utf-8")

    # Step 3 — Call LLM
    print(f"[apply_agent] Step 2: Calling {LLM_PROVIDER}/{LLM_MODEL}...")
    try:
        from llm_client import call_llm, LLMError
        content = call_llm(
            system_prompt=system_prompt,
            user_message=f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: {url}",
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
        )
    except LLMError as e:
        error_type = "rate_limit" if "rate" in str(e).lower() else "llm_error"
        notify(
            f"❌ <b>LLM failed ({error_type})</b>\n"
            f"URL: {url}\n"
            f"Model: {LLM_MODEL}\n\n"
            f"<pre>{str(e)[:500]}</pre>"
        )
        print(f"[apply_agent] LLM ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        notify(f"❌ <b>Unexpected error in LLM call</b>\n\n<pre>{str(e)[:500]}</pre>")
        print(f"[apply_agent] UNEXPECTED ERROR: {e}")
        sys.exit(1)

    # Step 4 — Validate JSON
    print("[apply_agent] Step 3: Validating LLM output...")
    errors = validate_content(content)
    if errors:
        print(f"[apply_agent] Validation errors: {errors}")
        notify(
            f"⚠️ <b>LLM output validation issues</b>\n"
            f"URL: {url}\n\n"
            + "\n".join(f"• {e}" for e in errors[:10])
        )
        # Continue anyway — partial content is better than nothing

    # Step 5 — Compute output folder and finalize JSON
    company = content.get("company_name", "Unknown")
    output_folder = compute_output_folder(company)
    output_folder.mkdir(parents=True, exist_ok=True)

    content["output_folder"] = str(output_folder).replace("\\", "/")
    content["apply_url"] = url
    if "ats_score" not in content:
        content["ats_score"] = ""

    # Step 6 — Write content.json
    content_path = output_folder / "content.json"
    content_path.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[apply_agent] Wrote {content_path}")

    # Step 7 — Run generate_docs.py
    gen_cmd = [sys.executable, str(GENERATE_DOCS_SCRIPT), str(content_path)]
    if _FULL_MODE:
        gen_cmd.append("--full")
    mode_label = "FULL" if _FULL_MODE else "SHORT"
    print(f"[apply_agent] Step 4: Generating docs ({mode_label})...")
    try:
        result = subprocess.run(
            gen_cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[generate_docs] STDERR:", result.stderr, file=sys.stderr)
        if result.returncode != 0:
            notify(
                f"⚠️ <b>generate_docs.py failed</b>\n"
                f"content.json written OK, but doc generation had issues.\n"
                f"Folder: <code>{output_folder.name}</code>"
            )
    except subprocess.TimeoutExpired:
        print("[apply_agent] generate_docs.py timed out (120s)")

    # Step 8 — Notify success
    created_files = list(output_folder.glob("*.docx")) + list(output_folder.glob("*.pdf"))
    if created_files:
        file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
        ats = content.get("ats_score", "?")
        notify(
            f"✅ <b>Docs ready!</b>\n\n"
            f"📁 <code>Applications/{output_folder.name}/</code>\n\n"
            f"{file_names}\n\n"
            f"ATS: {ats}% | Stack: {content.get('stack', '?')}\n"
            f"Via: API ({LLM_MODEL})\n"
            f"Review and send when ready."
        )
        print(f"\n[apply_agent] Done! Folder: Applications/{output_folder.name}/ ({len(created_files)} files)")
    else:
        notify(
            f"⚠️ <b>content.json OK but no docs generated</b>\n"
            f"📁 <code>Applications/{output_folder.name}/</code>\n"
            f"Run manually: python generate_docs.py \"{content_path}\""
        )
        print(f"\n[apply_agent] WARNING: No .docx/.pdf files found, but content.json is saved.")
        sys.exit(1)


# ══════════════════════════════════════════════════════════════════════════════
# CLI MODE — original Claude Code CLI approach (fallback)
# ══════════════════════════════════════════════════════════════════════════════

def _get_existing_folders() -> set[str]:
    if not APPLICATIONS_DIR.exists():
        return set()
    return {f.name for f in APPLICATIONS_DIR.iterdir() if f.is_dir()}


def _find_new_folder(before: set[str], timeout: int = 300) -> str | None:
    run_start = time.time()
    deadline = run_start + max(timeout, 0)
    while True:
        if APPLICATIONS_DIR.exists():
            for folder in APPLICATIONS_DIR.iterdir():
                if not folder.is_dir():
                    continue
                if folder.name not in before:
                    return folder.name
                if folder.stat().st_mtime >= run_start - 5:
                    return folder.name
        if time.time() >= deadline:
            break
        time.sleep(5)
    return None


def main_cli(url: str) -> None:
    print(f"\n[apply_agent] CLI mode | URL: {url}\n")

    if _already_processed(url):
        print(f"[apply_agent] SKIP — already in tracker: {url}")
        return

    folders_before = _get_existing_folders()

    notify(
        f"⏳ <b>Generating docs (CLI)...</b>\n"
        f"URL: {url}\n"
        f"This takes 1-2 minutes."
    )

    # Pre-fetch job text via JSON API so Claude doesn't have to WebFetch
    apply_input = url
    try:
        from job_fetch import fetch_job_text
        job_text = fetch_job_text(url)
        if job_text and len(job_text) > 100:
            apply_input = f"URL: {url}\n\n{job_text}"
            print(f"[apply_agent] Pre-fetched {len(job_text)} chars via JSON API")
    except Exception as e:
        print(f"[apply_agent] Pre-fetch failed ({e}), passing raw URL to Claude")

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

        # Permanent failure — not overloaded, or last attempt
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
            print(f"\n[apply_agent] Done! Folder: Applications/{new_folder}/ ({len(created_files)} files)")
        else:
            notify(
                f"⚠️ <b>Folder created but no docs found!</b>\n"
                f"📁 <code>Applications/{new_folder}/</code>"
            )
            print(f"\n[apply_agent] WARNING: Folder created but no .docx/.pdf files found.")
            raise ApplyError("Folder created but no docs found")
    else:
        notify(f"❌ <b>No output folder created!</b>\nURL: {url}")
        print(f"\n[apply_agent] FAIL: claude exited 0 but no new folder was created.")
        raise ApplyError("No output folder created")


# ══════════════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════════════

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


class ApplyError(RuntimeError):
    """Raised when an apply attempt fails and fallback should be tried."""


def main(url: str, force_cli: bool = False, force: bool = False, full: bool = False) -> None:
    if force:
        global _SKIP_DEDUP
        _SKIP_DEDUP = True
    if full:
        global _FULL_MODE
        _FULL_MODE = True

    if force_cli or APPLY_USE_CLI:
        main_cli(url)
        return

    cli_ok = _is_cli_available()
    if cli_ok:
        print("[apply_agent] Claude CLI detected (Pro subscription) — trying CLI first")
        try:
            main_cli(url)
            return
        except (ApplyError, SystemExit) as e:
            if LLM_API_KEY:
                print(f"[apply_agent] CLI failed ({e}), falling back to API mode")
            else:
                print(f"[apply_agent] CLI failed and no API key available")
                sys.exit(1)

    if LLM_API_KEY:
        main_api(url)
    else:
        print("[apply_agent] ERROR: No Claude CLI login and no LLM_API_KEY set. Cannot proceed.")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python apply_agent.py <job_url> [--cli] [--force] [--full]")
        sys.exit(1)

    force_cli = "--cli" in sys.argv
    force = "--force" in sys.argv
    full = "--full" in sys.argv
    url = [a for a in sys.argv[1:] if not a.startswith("--")][0]
    main(url, force_cli=force_cli, force=force, full=full)
