# CV Claim-Judge Plan — LLM-as-judge verification pass (Roadmap Phase C)

Status: M1–M3 IMPLEMENTED (shipping in `JUDGE_MODE=warn`); M4 rollout pending
Context: `docs/PROJECT_REVIEW_2026-06.md`, Phase C.

---

## Problem

The generation LLM fabricates claims despite the RED LINES in
`prompts/generation_rules.md`. Every incident so far produced one more
hand-written regex scrub in `hunter/apply_shared.py`:

| Incident (prod CV) | Scrub added |
|---|---|
| Employer's DORA/RODO/ISO credentials claimed as candidate's | `_strip_compliance_claims` (~150 lines) |
| "Fortune 500 clients" invented in EN+PL summaries | `_strip_prestige_claims` (~140 lines) |
| "term / synonym" gloss pairs left by ATS keyword mirroring | `_dedup_skill_glosses` (~100 lines) |

This is whack-a-mole: each regex covers exactly one phrasing family, breaks on
new LLM wording, and the next fabrication class (invented metrics? invented
team sizes? invented domains?) has no scrub until it ships in a real CV.

**Goal:** one cheap second-model pass that checks every claim in the generated
content against the two ground-truth sources we already have — the candidate
profile and the job posting — and returns a structured violations list. Closes
the *class* of fabrications, not one phrasing.

---

## Design

### New module: `hunter/claim_judge.py`

```python
@dataclass
class Violation:
    field: str        # dotted path, e.g. "resume_en.summary",
                      # "resume_en.experience[2].bullets[1]", "cover_letter_pl"
    quote: str        # verbatim substring of the offending text
    reason: str       # one line, human-readable
    severity: str     # "fabrication" | "exaggeration" | "style"

@dataclass
class JudgeReport:
    violations: list[Violation]
    passed: bool                  # no fabrication/exaggeration findings
    raw: dict                     # parsed judge JSON (for judge_report.json)

def judge_content(content: dict, job_text: str) -> JudgeReport: ...
def repair_content(content: dict, report: JudgeReport, job_text: str) -> tuple[dict, list[str]]: ...
```

- `judge_content` builds the judge prompt (see below), calls
  `llm_client.call_llm()` with `JUDGE_MODEL`, parses the JSON, validates each
  violation (the `quote` must actually occur verbatim in the named `field` —
  hallucinated findings are dropped with a log line).
- `repair_content` applies fixes (strategy below) and returns
  `(new_content, fix_log)` mirroring the scrub functions' contract
  (`tuple[dict, list[str]]`) so pipeline wiring is uniform.
- Best-effort like every other quality stage: any exception → log warning,
  continue with unjudged content. The judge must never be the reason an
  application fails.

### Judge prompt: `prompts/judge_rules.md` (new)

System prompt structure:

1. Role: "You are a strict fact-checker for a generated resume/cover letter."
2. Ground truth definition: the ONLY allowed sources of factual claims are
   (a) the candidate profile, (b) the base-CV bullets, (c) the job posting
   (for keyword mirroring of *technologies*, never of *achievements,
   client names, or prestige*).
3. Violation taxonomy with examples from the real incidents:
   - `fabrication`: claims with no basis in profile — invented clients
     ("Fortune 500"), invented certifications/compliance expertise (DORA/ISO),
     invented metrics/scale, invented employers or dates.
   - `exaggeration`: a real fact inflated — "familiar with X" turned into
     "N years leading X". **Allowed:** adjacent/posting-mentioned tech claimed
     with familiarity verbs ("worked with", "exposure to") — this is the
     project's deliberate policy for careful adjacent claims; the judge must
     NOT flag it.
   - `style`: gloss pairs, duplicated keywords, broken phrasing. Report-only,
     never auto-repaired by the judge (the deterministic scrubs own this).
4. Output schema: strict JSON
   `{"violations": [{"field", "quote", "reason", "severity"}]}` — `quote` must
   be copied verbatim (this enables deterministic validation and repair).
5. Language note: check `_pl` fields in Polish, `_en` in English; same rules.

User message: candidate profile (`prompts/candidate_profile.md`) + base CV for
the detected stack + job posting text + the content JSON fields under review.

Fields under review: `resume_en.summary`, `resume_en.skills`, every
`resume_en.experience[*].bullets[*]`, same for `resume_pl` when present,
`cover_letter_en`, `cover_letter_pl`, `about_me_en`, `about_me_pl`. The
`education`/`company`/`period`/`title` fields are verbatim-locked by
generation rules and validated elsewhere — excluded to keep the prompt small.

### Repair strategy (two tiers, deterministic first)

1. **Deterministic clause drop** (no LLM): for each validated violation, remove
   the `quote` from the field using the same sentence/clause-drop mechanics as
   `_strip_prestige_claims` (drop the containing clause; if the sentence
   becomes empty/broken, drop the sentence). Works for the common case where
   the fabrication is an additive embellishment.
2. **Targeted LLM rewrite** (one call, only if needed): if a deterministic drop
   would leave a field empty or structurally broken (e.g. the whole summary is
   one fabricated sentence), send ONE rewrite call listing the violations for
   the affected fields only, with the instruction "remove/correct these claims,
   change nothing else, return the same JSON schema for these fields".
3. **Post-repair guards** (reuse existing machinery, order matters):
   - `validate_content()` — the 7-role count must survive;
   - the language gate runs AFTER the judge in the pipeline, so any
     contamination introduced by a repair is caught by the existing
     enforce-gate.
4. One repair round (`JUDGE_MAX_REPAIR_ROUNDS=1` initially). If violations of
   severity `fabrication` survive the round: behavior depends on rollout stage
   (warn vs block, below).

### Config (`hunter/config.py` + `.env`)

| Variable | Default | Description |
|---|---|---|
| `JUDGE_ENABLED` | `true` | Master toggle for the judge pass |
| `JUDGE_MODEL` | `claude-haiku-4-5-20251001` | Judge model (cheap, independent of generator) |
| `JUDGE_MAX_REPAIR_ROUNDS` | `1` | Repair rounds before giving up |
| `JUDGE_MODE` | `warn` | `report` / `warn` / `block` (rollout stages) |

Provider/API key reuse `LLM_PROVIDER` / `LLM_API_KEY` — no new secrets.

### Pipeline integration

**API pipeline** (`hunter/apply_api.py`): new **Step 4.72**, inserted after the
prestige/gloss scrubs (end of current Step 4.7 block, ~line 421) and BEFORE the
Step 4.75 language gate (judge repairs can introduce language drift; the
existing gate must remain the last word on language):

```python
# Step 4.72 — Claim judge: verify every claim against profile + posting
try:
    from hunter.claim_judge import judge_content, repair_content
    report = judge_content(content, job_text)
    if not report.passed:
        content, fixes = repair_content(content, report, job_text)
        ...  # log fixes; JUDGE_MODE handling (warn notify / block sys.exit(0))
except Exception as e:
    print(f"[apply_agent] Warning: claim judge failed (continuing): {e}")
```

`judge_report.json` is written into the output folder next to `content.json`
whenever there are findings (audit trail for tuning the prompt).

**CLI pipeline** (`hunter/apply_cli.py`): in the existing post-process block
(~line 300) where the scrubs and the language gate already run on the
CLI-written `content.json` — insert the judge between the scrubs and
`enforce_language_separation`. Any judge fix joins `_scrub_fixes`, which
already triggers the content.json rewrite + doc regeneration; a `block`
verdict reuses the language gate's delete-docs-and-return path.

**Skip conditions:** `paste://no-url` flow still judged (job_text exists).
Force mode (`skip_dedup=True`) IS judged — force mode explicitly tells the
generator to weave in posting technologies, and the judge's posting-exception
already allows that; what stays banned even in force mode is invented clients,
metrics, and credentials.

### Cost & latency

One judge call per application: ~10–14K input tokens (profile ~3K + posting
~3K + content ~5K + rules ~1K), ~0.5–1K output. On Haiku that is well under a
cent per application; at the current volume (≤ `MAX_JOBS_PER_RUN`=10 per cycle,
3 cycles/day) — negligible. Latency +5–15 s inside a pipeline whose subprocess
timeout is 900 s.

---

## Implementation milestones

### M1 — Judge core (report-only), no pipeline wiring ✅ DONE (2026-06-12)

- [x] `prompts/judge_rules.md` — system prompt with the ground-truth definition,
      violation taxonomy (fabrication/exaggeration/style), and the adjacent-claims
      policy carve-out; examples seeded from the real Fortune 500 + DORA incidents.
- [x] `hunter/claim_judge.py`: `Violation`, `JudgeReport`, `judge_content()`,
      `_parse_violations()` (verbatim-quote check; the planned `_validate_violations`
      name) + `_resolve_path()` dotted-path resolver, `iter_judged_fields()` helper
      (public, not `_`-prefixed). Added `quote_survives()` for the cheap post-repair
      re-check the pipeline needs.
- [x] Config: `JUDGE_ENABLED`, `JUDGE_MODEL`, `JUDGE_MODE`,
      `JUDGE_MAX_REPAIR_ROUNDS` in `hunter/config.py`.
- [x] Tests (`tests/test_claim_judge.py`), `call_llm` mocked: field flattening,
      non-verbatim/unknown-field/bad-severity findings dropped, dotted-path
      resolution incl. `experience[i].bullets[j]`, judge exception → empty
      passing report.

### M2 — Repair tier ✅ DONE (2026-06-12)

- [x] `repair_content()`: connector-aware deterministic clause-drop (`_drop_quote`
      — keeps the honest preceding clause, e.g. "...300+ German banks and Fortune
      500 firms" → "...300+ German banks"), single targeted LLM rewrite
      (`_llm_rewrite`) for fields a drop would empty, `validate_content()`
      role-count guard that discards a structure-worsening repair.
      Note: `_drop_quote` is keyed off the exact judged `quote` rather than a term
      regex, so it's a sibling of `_scrub_prestige_text` not a literal reuse;
      extracting a shared clause-drop helper is left to roadmap Phase A.1.
- [x] Tests: drop preserves the honest clause + collapses commas; structure-
      worsening repair rejected (returns original); `style` severity never repaired;
      end-to-end judge→repair on a mocked Fortune 500 finding.

### M3 — Pipeline wiring (mode=report → warn) ✅ DONE (2026-06-12)

- [x] `apply_api.py` Step 4.72 + `judge_report.json` artifact (written in Step 6
      where `output_folder` exists).
- [x] `apply_cli.py` post-process insertion (fix → joins `_scrub_fixes` → existing
      rewrite + regen path; `block` reuses delete-docs+return).
- [x] Telegram notify on findings in `warn`/`block` mode (top-5 actionable, via
      `JudgeReport.telegram_summary`).
- [x] Unit + integration tests (28 total); both pipelines compile + ruff-clean;
      full suite 1311 green. (Function-level coverage mirrors the existing
      scrub-test convention — direct function tests, not heavy pipeline mocks.)
- [x] **M3 follow-up — live verification (2026-06-12).** `tools/preview_judge.py`
      added (runs scrubs + `run_judge_stage` on an existing content.json, one Haiku
      call, no regeneration). Validated on a real CLI-generated CV (Lumicode /
      solid.jobs, Angular fixture): the judge correctly flagged **Figma** as a
      fabrication (absent from the profile, woven in from the posting) and the
      repair dropped it from skills — a class no existing regex scrub covers,
      proving the Phase C premise. Surfaced "Architected a workflow-consolidation
      module" as an `exaggeration` (profile: "Built ...") without auto-dropping it.

#### M3 review fixes (2026-06-12)

The first live run exposed two issues, fixed before merge:

1. **False-positive auto-repair.** A run flagged `SonarQube` as `exaggeration` and
   the repair dropped it — but SonarQube IS in the profile (Venture Labs). Two
   mitigations: (a) `judge_rules.md` now states a technology named anywhere in the
   profile (incl. experience bullets / Stack lines) is a legitimate skill; (b)
   **auto-repair is now `fabrication`-only** (`REPAIR_SEVERITIES`), the
   high-precision class — `exaggeration` is surfaced but never auto-dropped during
   the warn rollout.
2. **`report` mode mutated content.** The plan specifies report = artifact only;
   the first wiring repaired regardless of mode. Centralised the mode logic in
   `run_judge_stage()` (report → no content change; warn/block → repair
   fabrications; block → abort on a surviving fabrication). Both pipelines now call
   the one helper; `repair_content` gained a `severities` filter. +6 tests
   (1317 total).

### M4 — Rollout & tightening

- [ ] Run 1–2 weeks in `JUDGE_MODE=warn`. Collect `judge_report.json` files.
- [ ] Review precision: every false positive becomes a prompt clarification in
      `judge_rules.md` (NOT a code change).
- [ ] When precision is trusted: flip default to `JUDGE_MODE=block` for
      `fabrication` severity only (mirrors the language gate: API
      `sys.exit(0)` + Telegram notice, CLI delete-docs-and-return).
- [ ] Revisit the regex scrubs: keep them (they're free and deterministic) but
      stop ADDING new ones — new fabrication classes are handled by
      `judge_rules.md` edits.
- [ ] Update CLAUDE.md (pipeline step 5c, config table, work log) in the same
      PR as each milestone.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| Judge hallucinates violations (false positives) | Verbatim-quote validation drops unverifiable findings; `warn` rollout before `block`; posting-exception + adjacent-claims policy spelled out in the prompt |
| Judge misses fabrications (false negatives) | Regex scrubs remain as first echelon; judge_report.json audit trail; few-shot from real incidents |
| Repair breaks structure (drops a role, empties summary) | `validate_content()` guard rejects the repair; language gate still runs after |
| Repair introduces PL into EN | Judge runs BEFORE the language enforce-gate by design |
| Cost/latency creep | Single Haiku call, one repair round, hard `JUDGE_MAX_REPAIR_ROUNDS` |
| Judge outage blocks applications | Best-effort try/except in both pipelines — never fatal |

## Success criteria

- Both archived prod incidents (prestige + compliance) are detected by the
  judge on replay with zero code-level scrub involvement.
- No new regex scrub functions added after M4.
- Zero blocked-by-false-positive applications during the `warn` period
  (measured via judge_report.json review).
