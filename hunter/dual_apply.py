"""hunter/dual_apply.py — Shadow (A/B comparison) generation.

When dual-apply mode is on (toggled via the /dual Telegram command), after the
primary "boevoy" apply produces its documents, ``run_shadow`` generates a second,
side-by-side set with the shadow profile (default ``deepseek-v3``) into a
``{primary_folder}/{shadow_name}/`` subfolder.

The shadow runs the SAME pipeline stages as the boevoy apply (generation →
ATS loop → scrubs → claim judge → language gate → render → independent PDF
verdict → verdict refine loop), with only the generator model swapped — so an
A/B run compares models, not pipelines. The judge and the verdict always use
the Anthropic JUDGE_* config on both sides (same yardstick).

The shadow is comparison-only:
  • NO tracker row (generate_docs runs with --no-tracker)
  • NO Telegram message / no document upload
  • NO Google Sheets / Drive mirror (those run in the bot layer off the tracker
    row, which the shadow never creates)

Generated CV / cover-letter filenames are suffixed with the ATS score the shadow
scored on the independent check, e.g.::

    Ihar_Petrasheuski_CV_Angular_2026_EN_ats88.pdf

so the boevoy set and the shadow set can be eyeballed side by side.

Best-effort throughout: every failure logs a warning and returns. The primary
application is already complete and is never touched by anything here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from hunter.config import GENERATE_DOCS_PATH, PROJECT_DIR
from hunter.services.apply_service import build_generate_docs_cmd

# Doc files that should carry the ATS-score suffix (CV + cover letters).
_SUFFIXABLE = ("*.pdf", "*.docx")
# Files that are not application documents — never suffixed.
_SKIP_SUFFIX_NAMES = {"job_posting.txt", "content.json", "judge_report.json"}


def _read_job_text(primary_folder: Path) -> str:
    """Read job_posting.txt and strip the leading 'URL: ...' header line."""
    jp = primary_folder / "job_posting.txt"
    raw = jp.read_text(encoding="utf-8")
    # job_posting.txt is written as "URL: <url>\n\n<body>" (or "URL: (none...)").
    if raw.startswith("URL:"):
        parts = raw.split("\n\n", 1)
        return parts[1].strip() if len(parts) == 2 else raw.strip()
    return raw.strip()


def _ats_suffix(content: dict) -> str:
    """'_ats88' for the doc filenames — prefers the independent PDF verdict
    (same judge model as the primary, so A/B filenames compare like-for-like);
    falls back to the deterministic ats_check score; '' if neither exists."""
    for key in ("ats_verdict", "ats_check"):
        try:
            score = content.get(key, {}).get("score")
            if score is not None:
                return f"_ats{round(float(score))}"
        except Exception:
            continue
    return ""


def _suffix_docs(folder: Path, suffix: str) -> int:
    """Append `suffix` before the extension of every generated doc. Returns count."""
    if not suffix:
        return 0
    renamed = 0
    for pattern in _SUFFIXABLE:
        for f in folder.glob(pattern):
            if f.name in _SKIP_SUFFIX_NAMES or suffix in f.stem:
                continue
            target = f.with_name(f"{f.stem}{suffix}{f.suffix}")
            try:
                f.replace(target)
                renamed += 1
            except OSError as e:
                print(f"[dual] could not rename {f.name}: {e}")
    return renamed


def run_shadow(primary_folder: Path | str, *, full_mode: bool = False) -> Path | None:
    """Generate a shadow comparison set for an already-completed primary apply.

    Parameters
    ----------
    primary_folder : the boevoy apply's output folder (contains job_posting.txt).
    full_mode      : mirror the primary's doc mode (DOCX+PDF, PL CV, About_Me).

    Returns the shadow subfolder on success, else None. Never raises.
    """
    from hunter.llm_profiles import (
        dual_enabled,
        get_active,
        set_override,
        shadow_profile,
    )

    if not dual_enabled():
        return None

    shadow = shadow_profile()
    if shadow is None:
        print("[dual] shadow profile unavailable (missing API key) — skipping")
        return None

    active = get_active()
    if shadow.name == active.name:
        print(f"[dual] shadow == active ({active.name}) — nothing to compare, skipping")
        return None

    primary_folder = Path(primary_folder)
    try:
        job_text = _read_job_text(primary_folder)
    except Exception as e:
        print(f"[dual] could not read job_posting.txt ({e}) — skipping shadow")
        return None
    if len(job_text) < 100:
        print("[dual] job text too short — skipping shadow")
        return None

    sub = primary_folder / shadow.name
    print(f"[dual] Shadow run: {shadow.name} ({shadow.model}) -> {sub}")

    set_override(shadow)
    try:
        return _generate_shadow(sub, job_text, primary_folder, full_mode=full_mode)
    except Exception as e:
        print(f"[dual] shadow generation failed (continuing): {e}")
        return None
    finally:
        set_override(None)


def _generate_shadow(
    sub: Path, job_text: str, primary_folder: Path, *, full_mode: bool
) -> Path | None:
    """Core shadow pipeline (override already active). Best-effort."""
    from llm_client import LLMError, call_llm
    from hunter.apply_api import _detect_stack_hint, _load_base_cv
    from hunter.apply_shared import (
        PROMPTS_DIR,
        _ats_check_loop,
        _dedup_skill_glosses,
        _strip_compliance_claims,
        _strip_prestige_claims,
        build_ats_keyword_checklist,
        build_pl_skip_instruction,
        validate_content,
    )
    from hunter.llm_profiles import get_active

    prof = get_active()  # == shadow profile (override is set)

    # System prompt: candidate profile + generation rules (same as apply_api).
    instructions = (PROMPTS_DIR / "generation_rules.md").read_text(encoding="utf-8")
    profile_path = PROMPTS_DIR / "candidate_profile.md"
    system_prompt = (
        profile_path.read_text(encoding="utf-8") + "\n\n---\n\n" + instructions
        if profile_path.exists()
        else instructions
    )

    stack_hint = _detect_stack_hint(job_text)
    base_cv = _load_base_cv(stack_hint)

    from hunter.config import GEN_SKIP_PL_FOR_EN
    from hunter.lang_guard import detect_posting_language
    posting_lang = detect_posting_language(job_text)
    pl_optional = GEN_SKIP_PL_FOR_EN and not full_mode and posting_lang == "EN"

    user_message = f"Here is the job posting to analyze:\n\n{job_text}\n\nOriginal URL: (shadow run)"
    user_message += build_ats_keyword_checklist(job_text)
    user_message += build_pl_skip_instruction(posting_lang, full_mode=full_mode)
    if base_cv:
        user_message += (
            f"\n\n---\n\n## Base CV — {stack_hint} Track "
            f"(use as starting point for bullets)\n\n{base_cv}"
        )

    print(f"[dual] Calling {prof.provider}/{prof.model}...")
    try:
        content = call_llm(
            system_prompt=system_prompt,
            user_message=user_message,
            provider=prof.provider,
            model=prof.model,
            api_key=prof.api_key,
        )
    except LLMError as e:
        print(f"[dual] shadow LLM call failed: {e}")
        return None

    # Validate + one best-effort repair pass (mirror of apply_api's logic).
    errors = validate_content(content, pl_optional=pl_optional)
    if errors:
        print(f"[dual] validation errors: {errors}")
        try:
            repaired = call_llm(
                system_prompt=system_prompt,
                user_message=(
                    "The JSON you returned has structural problems. Fix ALL issues "
                    "below and return the COMPLETE JSON again (same schema, every "
                    "field):\n" + "\n".join(f"- {e}" for e in errors)
                    + f"\n\nPrevious JSON:\n{json.dumps(content, ensure_ascii=False)}"
                ),
                provider=prof.provider,
                model=prof.model,
                api_key=prof.api_key,
            )
            if len(validate_content(repaired, pl_optional=pl_optional)) < len(errors):
                content = repaired
        except Exception as e:
            print(f"[dual] repair pass failed (using first pass): {e}")

    # ATS rewrite loop (uses get_active() -> shadow via the override).
    content = _ats_check_loop(content, job_text)

    # Content scrubs (parity with the boevoy pipeline).
    try:
        from hunter.resume_sanitizer import sanitize_content
        content = sanitize_content(content)
    except Exception as e:
        print(f"[dual] sanitizer failed (continuing): {e}")
    try:
        content, _ = _strip_compliance_claims(content)
        content, _ = _strip_prestige_claims(content, job_text)
        content, _ = _dedup_skill_glosses(content)
    except Exception as e:
        print(f"[dual] scrubs failed (continuing): {e}")

    # Claim judge — same stage as the boevoy pipeline (Step 4.72), so the A/B
    # compares like-for-like content: fabrications get the same auto-repair on
    # both sides. Never blocks and never notifies Telegram (this is a
    # comparison artifact, not an outgoing application) — JUDGE_MODE=block is
    # capped to "warn" here, same as verdict_refine._run_safety_stages.
    judge_report = None
    try:
        from hunter.config import JUDGE_ENABLED, JUDGE_MODE
        if JUDGE_ENABLED:
            from hunter.claim_judge import run_judge_stage
            _mode = "warn" if JUDGE_MODE == "block" else JUDGE_MODE
            _outcome = run_judge_stage(content, job_text, base_cv, enabled=True, mode=_mode)
            content = _outcome.content
            judge_report = _outcome.report
            for _v in judge_report.actionable:
                print(f"[dual] judge: [{_v.severity}] {_v.field}: {_v.reason}")
            for _fix in _outcome.fixes:
                print(f"[dual] judge-repair: {_fix}")
    except Exception as e:
        print(f"[dual] claim judge failed (continuing): {e}")

    # Language gate — clean contamination but never block (this is a comparison
    # artifact, not an outgoing application). posting_lang was already computed
    # above (before the LLM call, to drive the M4 skip-instruction) — reused
    # here unchanged rather than detected a second time.
    try:
        from hunter.apply_shared import enforce_language_separation
        content, _blocked, _report = enforce_language_separation(content)
    except Exception as e:
        print(f"[dual] language gate failed (continuing): {e}")

    # Carry the primary's apply_url so the shadow content.json is self-describing.
    apply_url = ""
    try:
        primary_cj = primary_folder / "content.json"
        if primary_cj.exists():
            apply_url = json.loads(primary_cj.read_text(encoding="utf-8")).get("apply_url", "")
    except Exception:
        pass

    sub.mkdir(parents=True, exist_ok=True)
    content["output_folder"] = str(sub).replace("\\", "/")
    content["apply_url"] = apply_url
    content["primary_lang"] = posting_lang
    content.setdefault("ats_score", "")

    content_path = sub / "content.json"
    content_path.write_text(
        json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (sub / "job_posting.txt").write_text(job_text, encoding="utf-8")

    # Audit trail — same artifact as the boevoy pipeline, so tools/judge_stats.py
    # and manual A/B review see the shadow's violations too.
    if judge_report is not None and judge_report.violations:
        try:
            (sub / "judge_report.json").write_text(
                json.dumps(judge_report.to_dict(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[dual] could not write judge_report.json: {e}")

    # Render docs — shadow run, so NO tracker row. _run_gen is the single
    # render invocation, shared with the refine loop's regen callback below
    # so the two can't drift (same cwd/encoding/timeout).
    gen_cmd = build_generate_docs_cmd(
        generate_docs_script=GENERATE_DOCS_PATH,
        content_json_path=content_path,
        use_full=full_mode,
        force=False,
        python_executable=sys.executable,
        no_tracker=True,
    )

    def _run_gen() -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            gen_cmd,
            cwd=str(PROJECT_DIR),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    try:
        result = _run_gen()
        if result.stdout:
            print(result.stdout)
        if result.stderr:
            print("[dual] generate_docs STDERR:", result.stderr, file=sys.stderr)
    except subprocess.TimeoutExpired:
        print("[dual] generate_docs timed out (120s) — keeping content.json only")

    # Independent PDF verdict — same judge as the primary (JUDGE_* config), so
    # the A/B filename suffixes compare like-for-like. set_override() does NOT
    # affect this call: run_llm_verdict reads hunter.config.JUDGE_* directly,
    # never llm_profiles. Runs BEFORE suffixing so the suffix can carry it.
    # Comparison-only: NO tracker stamp, NO Sheets, NO Telegram for the shadow.
    try:
        from hunter.ats_pdf_roundtrip import run_llm_verdict
        verdict = run_llm_verdict(folder=sub, job_text=job_text)
        if verdict is not None:
            # Verdict refine loop — mirror of the boevoy Step 7.7b, so the A/B
            # compares full-pipeline vs full-pipeline, not full vs one-shot.
            # The rewrite rounds resolve their model via get_active(), which
            # returns the SHADOW profile here (the override is active); the
            # per-round judge + re-verdicts stay on the Anthropic JUDGE_*
            # config regardless, so both sides are scored by the same
            # yardstick. Comparison-only: NO tracker stamps (no to_learn /
            # verdict / cost re-stamp — the shadow has no row). The regen
            # callback reuses gen_cmd, which is already --no-tracker and
            # never --force for a shadow.
            from hunter.config import ATS_VERDICT_MAX_REFINES, ATS_VERDICT_TARGET
            if (
                float(verdict.get("score") or 0) < ATS_VERDICT_TARGET
                and ATS_VERDICT_MAX_REFINES > 0
            ):
                print(
                    f"[dual] shadow verdict {verdict.get('score')}% < target "
                    f"{ATS_VERDICT_TARGET}% — running refine loop "
                    f"(max {ATS_VERDICT_MAX_REFINES} round(s))..."
                )
                from hunter.verdict_refine import refine_loop

                def _regen_shadow(_folder: Path) -> None:
                    _run_gen()

                content, verdict = refine_loop(
                    content, job_text, base_cv, sub, verdict,
                    regenerate_docs=_regen_shadow,
                    target=ATS_VERDICT_TARGET,
                    max_rounds=ATS_VERDICT_MAX_REFINES,
                )
            content["ats_verdict"] = verdict
            content_path.write_text(
                json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"[dual] shadow verdict: {verdict.get('score')}%")
    except Exception as e:
        print(f"[dual] shadow verdict failed (continuing): {e}")

    # Suffix the rendered docs with the ATS score for at-a-glance comparison.
    suffix = _ats_suffix(content)
    n = _suffix_docs(sub, suffix)
    print(f"[dual] Shadow done: {n} doc(s){' ' + suffix if suffix else ''} in {sub}")

    # Best-effort Drive upload, nested under the primary's company folder
    # (Job Hunter/{date}/{company}/{shadow_name}/). No tracker row exists for
    # the shadow, so this is the only path its files reach Drive.
    try:
        from hunter.config import GDRIVE_ENABLED
        if GDRIVE_ENABLED:
            import asyncio
            from hunter.gdrive_sync import upload_shadow_folder
            url = asyncio.run(upload_shadow_folder(primary_folder, sub))
            if url:
                print(f"[dual] uploaded to Drive: {url}")
    except Exception as e:
        print(f"[dual] drive upload failed (continuing): {e}")

    return sub


# ── Detached launcher ───────────────────────────────────────────────────────────

def launch_detached(primary_folder: Path | str, *, full_mode: bool = False) -> bool:
    """Fire-and-forget the shadow run in its OWN process and return immediately.

    Called by apply_agent.main() after the primary apply succeeds. Running detached
    means the shadow can never affect the primary process's exit code or the bot's
    APPLY_AGENT_TIMEOUT_SEC — the primary has already committed its docs + tracker
    row by this point. No-op (returns False) when dual mode is off. Best-effort:
    any launch error is swallowed and returns False.
    """
    try:
        from hunter.llm_profiles import dual_enabled
        if not dual_enabled():
            return False
    except Exception:
        return False

    cmd = [sys.executable, "-m", "hunter.dual_apply", str(primary_folder)]
    if full_mode:
        cmd.append("--full")

    # Detach so the shadow outlives the parent and is never tied to its exit
    # code / timeout. Platform-specific flags keep it fully decoupled.
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    try:
        if sys.platform == "win32":
            kwargs["creationflags"] = (
                getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        print(f"[dual] shadow launched (detached) for {primary_folder}")
        return True
    except Exception as e:
        print(f"[dual] shadow launch failed: {e}")
        return False


# ── Detached CLI entry point ────────────────────────────────────────────────────
# Launched fire-and-forget by launch_detached() AFTER the primary apply finishes:
#     python -m hunter.dual_apply <primary_folder> [--full]
# Running in its own process means the shadow can NEVER affect the primary apply's
# exit code or the bot's APPLY_AGENT_TIMEOUT_SEC — the primary subprocess has already
# returned by the time this runs. A watchdog hard-caps the shadow's own runtime.

def _main(argv: list[str]) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = [a for a in argv[1:] if a]
    full_mode = "--full" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("Usage: python -m hunter.dual_apply <primary_folder> [--full]")
        return 2
    primary_folder = positional[0]

    # Watchdog: force-exit if the shadow overruns its own budget, so a hung LLM
    # call or doc render can't leave an orphan running forever.
    try:
        from hunter.config import DUAL_SHADOW_TIMEOUT_SEC
        budget = max(60, int(DUAL_SHADOW_TIMEOUT_SEC))
    except Exception:
        budget = 900

    import threading

    def _kill() -> None:
        print(f"[dual] watchdog: shadow exceeded {budget}s budget — exiting")
        os._exit(0)  # noqa: SLF001 — hard stop a detached best-effort process

    timer = threading.Timer(budget, _kill)
    timer.daemon = True
    timer.start()
    try:
        run_shadow(primary_folder, full_mode=full_mode)
    except Exception as e:  # noqa: BLE001 — best-effort, never surface
        print(f"[dual] shadow run failed: {e}")
    finally:
        timer.cancel()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv))
