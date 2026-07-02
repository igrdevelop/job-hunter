# Dual-Apply Shadow Parity Plan — CL review + claim judge for shadow runs

**Branch:** `feat/dual-shadow-parity` (from `origin/master` @ 1f969ed)
**Status:** PLANNED — ready for implementation
**Scope:** `hunter/dual_apply.py`, `hunter/apply_shared.py` (one small param), tests, CLAUDE.md

---

## Why (context from the 2026-07-02 A/B review)

The owner compared 8 primary (Sonnet) vs shadow (deepseek-v3) sets in
`G:\My Drive\Job Hunter\2026-07-02`. Two systematic asymmetries were found, and both
trace to pipeline stages the shadow run **skips**, not to the prompts (prompts are
identical — same system prompt, same user-message template, same base CV, same
`_ats_check_loop` + scrubs + lang gate):

1. **No claim judge on shadows.** DeepSeek mirrored posting keywords into first-person
   experience claims that the judge would have caught on the primary:
   - KuehneNagel: "10+ years … Angular, TypeScript, Java, **and PHP**", "Laravel
     frameworks", "experience in Sea/Air Logistics systems" (PHP/Laravel absent from
     `prompts/candidate_profile.md`; all present in the posting).
   - Etteplan: summary rewritten as "10+ years … React, Next.js, Vue.js, **Svelte**",
     Angular buried at the end of skills.
   - RTBHouse: "systems handling **over 20 million requests per second**" (that's RTB
     House's own scale, from the posting) + VAST/MRAID/OpenRTB stuffed into skills.
   Primary folders contain `judge_report.json`; shadow folders never do — the judge
   stage simply isn't wired into `_generate_shadow()`.

2. **No cover-letter review on shadows.** Primary runs `_cover_letter_review()`
   (quality gates + rewrite); the shadow CL is the raw first draft. Result: shadow CLs
   are consistently shorter (1.2–1.5k chars vs 1.6–2.1k) and more template-like. The
   CL comparison is currently unfair **against** the shadow model.

Goal: make the shadow pipeline stage-for-stage equal to the primary so the A/B
comparison is like-for-like, while keeping the shadow's contract: **best-effort,
comparison-only, NO tracker row, NO Telegram, NO Sheets** (Drive upload of the shadow
subfolder stays as is).

Target stage order in `_generate_shadow()` (mirrors `apply_api`):

```
call_llm → validate/repair → _ats_check_loop
    → [NEW] _cover_letter_review(quiet=True)          # M1+M2
    → sanitize → scrubs (compliance/prestige/glosses)
    → [NEW] run_judge_stage(..., never "block")       # M3
    → lang gate → generate_docs → PDF verdict → suffix → Drive
```

---

## M1 — `quiet` mode for `_cover_letter_review` (apply_shared.py)

`hunter/apply_shared.py:1440` — `_cover_letter_review(content)` sends a Telegram
message via `notify()` when the letter gets rewritten (line ~1458). The shadow must
never message Telegram.

- Change signature to `_cover_letter_review(content: dict, *, quiet: bool = False)`.
- When `quiet=True`, skip ONLY the `notify(...)` call; keep the `print` logging and
  the PL re-translation (`_translate_cover_letter_pl`) exactly as is.
- Default behaviour (primary pipelines, `_cover_letter_review_loop` shim) unchanged.

## M2 — wire CL review into the shadow (dual_apply.py)

In `_generate_shadow()` insert **after** `content = _ats_check_loop(content, job_text)`
(currently line ~211) and **before** the sanitize block:

```python
# Cover-letter review (parity with the boevoy pipeline). The llm_profiles
# override is active, so the review + PL re-translation run on the SHADOW
# model — that's the point of the A/B. quiet=True: no Telegram from a shadow.
try:
    from hunter.apply_shared import _cover_letter_review
    content = _cover_letter_review(content, quiet=True)
except Exception as e:
    print(f"[dual] CL review failed (continuing): {e}")
```

Notes:
- `_review_cover_letter` → `call_llm` resolves the model via `get_active()`, which
  returns the shadow profile while the override is set. No extra plumbing needed.
  Do NOT force the judge/Anthropic model here — the CL review is part of the
  generation being compared, so it must run on the shadow model.
- Best-effort: any exception logs and continues (shadow contract).

## M3 — wire the claim judge into the shadow (dual_apply.py)

In `_generate_shadow()` insert **after** the scrubs `try` block (currently ends
line ~224) and **before** the lang-gate block:

```python
# Claim judge (parity with the boevoy pipeline). run_judge_stage reads
# JUDGE_PROVIDER/JUDGE_MODEL from config directly — unaffected by
# set_override() — so primary and shadow are judged by the SAME yardstick.
# A shadow is a comparison artifact: never block, never notify Telegram.
judge_report = None
try:
    from hunter.config import JUDGE_ENABLED, JUDGE_MODE
    if JUDGE_ENABLED:
        from hunter.claim_judge import run_judge_stage
        shadow_mode = "warn" if JUDGE_MODE == "block" else JUDGE_MODE
        _outcome = run_judge_stage(
            content, job_text, base_cv, enabled=True, mode=shadow_mode
        )
        content = _outcome.content
        judge_report = _outcome.report
        for _v in judge_report.actionable:
            print(f"[dual] judge: [{_v.severity}] {_v.field}: {_v.reason}")
        for _fix in _outcome.fixes:
            print(f"[dual] judge-repair: {_fix}")
except Exception as e:
    print(f"[dual] claim judge failed (continuing): {e}")
```

- Mode mapping: `report`→`report`, `warn`→`warn`, `block`→`warn` (capped). A shadow
  never aborts: with `mode="warn"` repairs are applied and survivors just reported.
- No `notify()` calls at all (unlike apply_api Step 4.72).
- `base_cv` is already in scope (loaded at line ~166).

### M3b — persist `judge_report.json` in the shadow folder

After `content_path.write_text(...)` / `job_posting.txt` write (currently line ~256),
add (mirroring apply_api's audit-trail write):

```python
if judge_report is not None and judge_report.violations:
    try:
        (sub / "judge_report.json").write_text(
            json.dumps(judge_report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"[dual] could not write judge_report.json: {e}")
```

- `sub.mkdir()` already happens before this point — no reordering needed.
- `judge_report.json` is already in `_SKIP_SUFFIX_NAMES`, so the `_ats{NN}` renamer
  won't touch it.

## M4 — tests

Extend `tests/test_dual_apply.py` (follow its existing mock style — the suite mocks
`call_llm`, `_ats_check_loop`, subprocess, etc.). New cases:

1. Shadow calls `_cover_letter_review` once with `quiet=True`; no `notify` fired.
2. `_cover_letter_review` raising → shadow still completes (best-effort).
3. `JUDGE_ENABLED=True, JUDGE_MODE="block"` → `run_judge_stage` called with
   `mode="warn"` (cap verified); repaired content flows into content.json.
4. `JUDGE_ENABLED=False` → `run_judge_stage` never called.
5. Judge outcome with violations → `judge_report.json` written into the shadow
   subfolder with the report dict.
6. `run_judge_stage` raising → shadow still completes.

Extend the `_cover_letter_review` tests (wherever apply_shared CL tests live,
e.g. `tests/test_apply_shared*.py`):

7. `quiet=True` + rewrite happened → `notify` NOT called, PL re-translation still runs.
8. Default (`quiet` omitted) + rewrite → `notify` called (regression guard).

## M5 — docs (same commit)

- CLAUDE.md: update the "Dual-apply (A/B model comparison)" section — the shadow now
  runs `_cover_letter_review` (on the shadow model, quiet) and the claim judge
  (same Anthropic `JUDGE_*` judge as the primary; `block` capped to `warn`; writes
  `judge_report.json` into the shadow folder). Add an Agent Work Log entry.
- This plan file: flip Status to DONE.

---

## Explicitly OUT of scope

- `claim_judge` repair's LLM-rewrite fallback calls `call_llm` with config
  `LLM_PROVIDER/LLM_MODEL` (claim_judge.py ~line 417), not `llm_profiles.get_active()`
  — so a shadow's rare rewrite-fallback would run on the primary's model. Acceptable:
  the rewrite only REMOVES unsupported claims (never generates new material), and the
  deterministic clause-drop handles most repairs. Do not change in this PR; note it in
  the work log if convenient.
- No new config vars. Reuse `JUDGE_ENABLED/JUDGE_MODE/JUDGE_MODEL/JUDGE_PROVIDER`.
- No re-run/backfill of existing shadow folders on Drive.

## Budget / runtime impact per shadow

- +1 Haiku judge call (~$0.01) + repair calls only when fabrications found.
- +1–2 shadow-model calls for CL review/rewrite + PL re-translation (DeepSeek-V3 —
  fractions of a cent).
- +30–60 s wall clock — fits comfortably in `DUAL_SHADOW_TIMEOUT_SEC` (900 s watchdog).

## Acceptance criteria

- [ ] `pytest tests/` green (incl. 8 new tests), `ruff check .` clean,
      `python -m compileall .` OK.
- [ ] Shadow pipeline order matches primary: ATS loop → CL review → scrubs → judge →
      lang gate.
- [ ] No Telegram message can originate from a shadow run (grep `notify(` in
      dual_apply.py stays empty; CL review called with `quiet=True`).
- [ ] Shadow never blocks/aborts on judge findings; `judge_report.json` appears in the
      shadow subfolder when violations are found.
- [ ] Primary pipelines behaviourally unchanged (default `quiet=False`).
