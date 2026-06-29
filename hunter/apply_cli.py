"""
hunter/apply_cli.py — CLI pipeline for apply_agent.

Uses `claude -p --dangerously-skip-permissions /apply <input>` (Claude Pro subscription).
Falls back to API mode if CLI is unavailable or errors (handled by apply_agent.main).

Public entry points:
    main_cli(url, *, skip_dedup, full_mode) -> None
    _is_cli_available() -> bool
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from hunter.apply_shared import (
    ApplyError,
    _REACT_SKIP_FORCE_HINT,
    _already_processed,
    notify,
    send_telegram_documents,
)
from hunter.config import (
    APPLICATIONS_DIR,
    CLI_MAX_RETRIES,
    CLI_RETRY_DELAY,
    GENERATE_DOCS_PATH,
    PROJECT_DIR,
)
from hunter.services.apply_service import build_generate_docs_cmd


# ── Folder detection helpers ──────────────────────────────────────────────────

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
    """Detect a newly created application folder after the Claude CLI runs.

    Searches today's date subfolder first (new structure), then falls back to
    scanning Applications/ directly (legacy / CLI created outside date dir).
    Returns a relative path like "2026-04-14/CompanyName" (new) or plain folder
    name (legacy), or None if nothing new is found within timeout seconds.
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


# ── CLI availability check ────────────────────────────────────────────────────

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


# ── CLI pipeline ──────────────────────────────────────────────────────────────

def main_cli(
    url: str,
    *,
    skip_dedup: bool = False,
    full_mode: bool = False,
    paste_text: str = "",
) -> Path | None:
    """CLI pipeline: pre-fetch job text → run `claude -p /apply` → post-process.

    Returns the output folder on success (so the caller can run the dual-apply
    shadow), or None when the job was skipped / deduped / expired / blocked.

    Parameters
    ----------
    url:        Job URL to process (may be PASTE_NO_URL_PLACEHOLDER when paste_text set).
    skip_dedup: When True, bypass tracker dedup check.
    full_mode:  When True, pass --full to generate_docs.py (DOCX + PDF, PL CV).
    paste_text: Pre-supplied job text (skips HTTP fetch). CLI receives it directly.

    Raises ApplyError on failure so apply_agent.main() can try API fallback.
    """
    url_display = url if url and "paste://" not in url else "(pasted text, no URL)"
    print(f"\n[apply_agent] CLI mode | URL: {url_display}\n")

    if not paste_text and _already_processed(url, skip_dedup=skip_dedup):
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

    # Determine apply_input for the CLI skill:
    # - paste_text provided → use it directly (no HTTP fetch needed)
    # - URL provided → pre-fetch via JSON API so Claude CLI doesn't have to WebFetch
    apply_input: str
    job_text: str | None = None

    if paste_text:
        apply_input = paste_text
        job_text = paste_text
        print(f"[apply_agent] Using pasted text ({len(paste_text)} chars) — skipping fetch")
    else:
        apply_input = url
        try:
            from hunter.sources import fetch_job_text
            job_text = fetch_job_text(url)
            if job_text and len(job_text) > 100:
                apply_input = f"URL: {url}\n\n{job_text}"
                print(f"[apply_agent] Pre-fetched {len(job_text)} chars via JSON API")
        except Exception as e:
            print(f"[apply_agent] Pre-fetch failed ({e}), passing raw URL to Claude")

    # Check for expired offer before spinning up Claude CLI
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

        # Manual-apply "warn but allow" screen (see apply_api Step 1.5e).
        try:
            from hunter.filters import screen_job_text
            screen_reason = screen_job_text(job_text)
            if screen_reason:
                notify(
                    f"⚠️ <b>Heads-up — this posting would normally be filtered</b>\n"
                    f"Reason: {screen_reason}\n"
                    f"🔗 {url}\n\n"
                    f"Generating documents anyway (manual override)…"
                )
                print(f"[apply_agent] WARN (manual screen) — {screen_reason}: {url}")
        except Exception as e:  # noqa: BLE001 — best-effort, never block apply
            print(f"[apply_agent] Warning: manual screen failed: {e}")

    cmd = ["claude", "-p", "--dangerously-skip-permissions", f"/apply {apply_input}"]
    print("[apply_agent] Running claude CLI...\n")

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
                env=os.environ,
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
                print("\n[apply_agent] Timeout — no folder created.")
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

        # Save raw job posting text (free — no LLM required)
        if job_text:
            try:
                job_posting_path = folder_path / "job_posting.txt"
                job_posting_path.write_text(f"URL: {url}\n\n{job_text}", encoding="utf-8")
                print(f"[apply_agent] Saved job posting -> {job_posting_path.name}")
            except Exception as e:
                print(f"[apply_agent] Warning: could not save job_posting.txt: {e}")

        # Post-process content.json written by Claude: React-only skip + CL review
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

                # Language enforce-gate (parity with the API pipeline). The CLI skill
                # already generated docs; if any _en field leaked Polish, repair the
                # content and REGENERATE the docs from the cleaned content.json — or, if
                # strong Polish can't be removed, delete the docs and block delivery so a
                # contaminated CV is never sent.
                try:
                    from hunter.lang_guard import detect_posting_language
                    from hunter.apply_shared import (
                        _dedup_skill_glosses,
                        _strip_prestige_claims,
                        enforce_language_separation,
                    )
                    # Deterministic scrubs (parity with the API pipeline): drop
                    # fabricated prestige claims + collapse skills gloss pairs.
                    # Any fix means the already-generated docs are stale and must
                    # be regenerated below, same as a language-gate repair.
                    _scrub_fixes: list[str] = []
                    _cli_content, _prestige_fixes = _strip_prestige_claims(
                        _cli_content, job_text or ""
                    )
                    _scrub_fixes.extend(_prestige_fixes)
                    _cli_content, _gloss_fixes = _dedup_skill_glosses(_cli_content)
                    _scrub_fixes.extend(_gloss_fixes)
                    for _line in _scrub_fixes:
                        print(f"[apply_agent] content-scrub: {_line}")

                    # Claim judge (parity with the API pipeline): verify claims
                    # against profile + posting between the scrubs and the language
                    # gate. Any repair joins _scrub_fixes → triggers the rewrite +
                    # doc-regeneration path below. A surviving fabrication in
                    # JUDGE_MODE=block deletes the docs and aborts.
                    from hunter.config import JUDGE_ENABLED, JUDGE_MODE
                    if JUDGE_ENABLED:
                        try:
                            from hunter.claim_judge import run_judge_stage
                            _outcome = run_judge_stage(
                                _cli_content, job_text or "", enabled=True, mode=JUDGE_MODE
                            )
                            _cli_content = _outcome.content
                            if _outcome.report.violations:
                                try:
                                    (folder_path / "judge_report.json").write_text(
                                        json.dumps(
                                            _outcome.report.to_dict(),
                                            ensure_ascii=False, indent=2,
                                        ),
                                        encoding="utf-8",
                                    )
                                except OSError:
                                    pass
                            for _line in _outcome.fixes:
                                print(f"[apply_agent] judge-repair: {_line}")
                            _scrub_fixes.extend(_outcome.fixes)
                            if JUDGE_MODE in ("warn", "block") and _outcome.report.actionable:
                                notify(_outcome.report.telegram_summary(url))
                            if _outcome.blocked:
                                for _f in (
                                    list(folder_path.glob("*.pdf"))
                                    + list(folder_path.glob("*.docx"))
                                ):
                                    try:
                                        _f.unlink()
                                    except OSError:
                                        pass
                                notify(
                                    f"⛔ <b>Blocked — fabricated claim survived repair</b>\n"
                                    f"🔗 {url}\n"
                                    + "\n".join(
                                        f"• {v.field}: {v.reason[:100]}"
                                        for v in _outcome.survivors[:3]
                                    )
                                )
                                print("[apply_agent] ABORT — claim judge blocked delivery (CLI)")
                                return
                        except Exception as _je:
                            print(f"[apply_agent] Warning: claim judge failed (continuing): {_je}")

                    _posting_lang = detect_posting_language(job_text or "")
                    _cli_content, _blocked, _report = enforce_language_separation(_cli_content)
                    for _line in _report:
                        print(f"[apply_agent] lang-gate: {_line}")
                    if _report or _scrub_fixes:  # repaired/scrubbed something and/or blocked
                        _cli_content["primary_lang"] = _posting_lang
                        content_json_path.write_text(
                            json.dumps(_cli_content, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        if _blocked:
                            for _f in (
                                list(folder_path.glob("*.pdf")) + list(folder_path.glob("*.docx"))
                            ):
                                try:
                                    _f.unlink()
                                except OSError:
                                    pass
                            notify(
                                f"⛔ <b>Blocked — Polish leaked into the English CV</b>\n"
                                f"🔗 {url}\n"
                                f"The English documents still contained Polish after an "
                                f"automatic translation pass, so they were NOT sent. "
                                f"Re-run /force to retry, or apply manually."
                            )
                            print("[apply_agent] ABORT — language gate blocked delivery (CLI)")
                            return
                        # Remove the pre-gate (contaminated) docs FIRST, so a failed
                        # regeneration (e.g. LibreOffice down) can't leave a stale
                        # contaminated PDF behind for created_files to pick up and send.
                        for _stale in (
                            list(folder_path.glob("*.pdf")) + list(folder_path.glob("*.docx"))
                        ):
                            try:
                                _stale.unlink()
                            except OSError:
                                pass
                        # Regenerate docs from the cleaned content.json.
                        _gen_cmd = build_generate_docs_cmd(
                            generate_docs_script=GENERATE_DOCS_PATH,
                            content_json_path=content_json_path,
                            use_full=full_mode,
                            force=skip_dedup,
                            python_executable=sys.executable,
                        )
                        subprocess.run(
                            _gen_cmd,
                            cwd=str(PROJECT_DIR),
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            errors="replace",
                            timeout=120,
                        )
                        print("[apply_agent] lang-gate: regenerated docs from cleaned content")
                except Exception as _lang_err:
                    print(f"[apply_agent] Warning: CLI language gate failed (continuing): {_lang_err}")

            except Exception as e:
                print(f"[apply_agent] CLI post-processing error: {e}")

        # PDF roundtrip + NBSP self-heal — mirror of the API pipeline.
        # See hunter/apply_api.py for the full rationale: re-score the
        # rendered EN CV PDF, and if Δ ≥ HEAL_DELTA_PP below the JSON score
        # patch each multi-word missing keyword with NBSP and regen once.
        # Best-effort — failures log + continue, never block delivery.
        pdf_summary = ""
        if job_text:
            try:
                from hunter.ats_pdf_roundtrip import (
                    HEAL_DELTA_PP,
                    format_summary,
                    nbsp_patch_missing_keywords,
                    run_pdf_roundtrip,
                )
                try:
                    _cli_content_for_score = json.loads(
                        content_json_path.read_text(encoding="utf-8")
                    )
                    _json_score = _cli_content_for_score.get("ats_score")
                except Exception:
                    _cli_content_for_score = None
                    _json_score = None

                pdf_check = run_pdf_roundtrip(
                    folder=folder_path,
                    job_text=job_text,
                    json_ats_score=_json_score,
                )

                delta = pdf_check.get("delta_from_json") if pdf_check else None
                if (
                    pdf_check
                    and _cli_content_for_score is not None
                    and delta is not None
                    and delta <= -HEAL_DELTA_PP
                ):
                    missing = pdf_check.get("missing_keywords") or []
                    patches = nbsp_patch_missing_keywords(_cli_content_for_score, missing)
                    if patches:
                        print(
                            f"[apply_agent] PDF Δ={delta:+.1f}pp — "
                            f"patched {patches} multi-word keyword(s) with NBSP, regenerating"
                        )
                        content_json_path.write_text(
                            json.dumps(_cli_content_for_score, ensure_ascii=False, indent=2),
                            encoding="utf-8",
                        )
                        _heal_cmd = build_generate_docs_cmd(
                            generate_docs_script=GENERATE_DOCS_PATH,
                            content_json_path=content_json_path,
                            use_full=full_mode,
                            force=skip_dedup,
                            python_executable=sys.executable,
                        )
                        try:
                            subprocess.run(
                                _heal_cmd,
                                cwd=str(PROJECT_DIR),
                                capture_output=True,
                                text=True,
                                encoding="utf-8",
                                errors="replace",
                                timeout=120,
                            )
                            pdf_check_2 = run_pdf_roundtrip(
                                folder=folder_path,
                                job_text=job_text,
                                json_ats_score=_json_score,
                            )
                            if pdf_check_2 is not None:
                                pdf_check = pdf_check_2
                        except subprocess.TimeoutExpired:
                            print("[apply_agent] self-heal regen timed out (120s) — keeping original PDF")

                if pdf_check is not None and _cli_content_for_score is not None:
                    _cli_content_for_score["ats_check_pdf"] = pdf_check
                    content_json_path.write_text(
                        json.dumps(_cli_content_for_score, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    pdf_summary = "\n" + format_summary(pdf_check)
                    print(f"[apply_agent] {format_summary(pdf_check)}")
            except Exception as e:
                print(f"[apply_agent] Warning: PDF roundtrip failed (continuing): {e}")

        # Stamp a "mode=cli" cost record on content.json so the tracker /
        # Sheets column stays consistent across API and CLI runs. The CLI
        # runs through the Claude Pro subscription — we have no per-token
        # visibility, and dividing $20/month by call count would be
        # misleading. total_usd=None means "not measured" downstream.
        try:
            _cli_content = json.loads(content_json_path.read_text(encoding="utf-8"))
            _cli_content["cost"] = {"mode": "cli", "total_usd": None}
            content_json_path.write_text(
                json.dumps(_cli_content, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[apply_agent] Warning: could not stamp CLI cost record: {e}")

        created_files = list(folder_path.glob("*.docx")) + list(folder_path.glob("*.pdf"))
        if created_files:
            file_names = "\n".join(f"  • {f.name}" for f in sorted(created_files))
            notify(
                f"✅ <b>Docs ready!</b>\n\n"
                f"📁 <code>Applications/{new_folder}/</code>\n\n"
                f"{file_names}\n"
                f"{pdf_summary}\n"
                f"Via: CLI (Pro subscription)\n"
                f"Cost: included in Pro plan\n"
                f"Review and send when ready."
            )
            send_telegram_documents(created_files)
            print(f"\n[apply_agent] Done! Folder: Applications/{new_folder}/ ({len(created_files)} files)")
            # Success: return the folder so apply_agent.main() can run the
            # dual-apply shadow comparison (if enabled).
            return folder_path
        else:
            notify(
                f"⚠️ <b>Folder created but no docs found!</b>\n"
                f"📁 <code>Applications/{new_folder}/</code>\n"
                f"Check the folder for partial output."
            )
            print("\n[apply_agent] WARNING: Folder created but no .docx/.pdf files found.")
            raise ApplyError("Folder created but no docs found")
    else:
        stdout_preview = (result.stdout or "").strip()[:600] if result else ""
        notify(
            f"❌ <b>CLI exited 0 but no folder created</b>\n"
            f"🔗 {url}\n\n"
            + (f"Claude output:\n<pre>{stdout_preview}</pre>" if stdout_preview else "No CLI output captured.")
        )
        print("\n[apply_agent] FAIL: claude exited 0 but no new folder was created.")
        raise ApplyError("No output folder created")
