# ATS Verdict — Phase 2: Google Sheets column + dual-apply shadow coverage

**Status:** PLANNED (this document is the implementation spec)
**Audience:** implementing agent (Claude Code or similar) — self-contained, no chat context needed
**Prerequisite:** PR #112 (`feat/ats-deterministic-loop-pdf-verdict`) **must be merged into master first**.
Branch for this work AFTER it merges: `git fetch origin && git checkout -b feat/ats-verdict-phase2 origin/master`.
If #112 is not merged yet, STOP and wait (or coordinate with the owner) — every step below
builds on symbols introduced there.

---

## 1. Background (what exists after PR #112)

Phase 1 (PR #112) fixed the biggest LLM-spend leak and introduced the independent verdict:

- **Deterministic ATS rewrite loop** — `hunter/apply_shared.py::_ats_check_loop` exits as soon
  as the blocklist-filtered missing-keyword list is empty. No LLM review runs inside the loop
  (pure regex keyword match + TF-IDF). Rationale: prod data (713 `content.json` on Drive)
  showed 88% of June–July runs burning all 5 rewrite rounds with `keyword_score` already 100%,
  because the combined threshold (95%) was mathematically unreachable — the post-round-1
  formula `keyword×0.75 + TF-IDF×0.25` is capped by TF-IDF (median 51, needs ≥80), which no
  rewrite moves.
- **Final independent LLM verdict** — `hunter/ats_checker.py::llm_verdict()` (one call,
  caps: job 6000 / resume 9000 chars) invoked via
  `hunter/ats_pdf_roundtrip.py::run_llm_verdict(folder, job_text)`. It extracts text from the
  **rendered EN CV PDF** (`find_en_cv_pdf` + `extract_pdf_text`) — i.e. what a real ATS
  actually parses — and scores it with the **judge model** (`JUDGE_MODEL` / `JUDGE_PROVIDER` /
  `JUDGE_API_KEY` from `hunter/config.py`, default Haiku), a model that did NOT write the
  resume. Result dict: `{"score", "missing_keywords", "recommendations", "gap_report",
  "model", "pdf_file"}` or `None` (best-effort, never blocks).
- **Wiring** — `hunter/apply_api.py` Step 7.7 (stores `content["ats_verdict"]`, re-prices
  cost) and `hunter/apply_cli.py` (after the PDF roundtrip block, persists `ats_verdict`
  into `content.json`). Telegram success message leads with the verdict:
  `ATS: 91% (independent, PDF) | self: 97%`.
- **Config** — `ATS_VERDICT_ENABLED` (default `true`) in `hunter/config.py`.

## 2. Phase 2 goals

| # | Goal | Why |
|---|------|-----|
| G1 | Mirror the verdict score to **Google Sheets** (its own column, like Cost) | The owner reviews applications in the Sheet; the verdict must be visible there, not only in a transient Telegram message |
| G2 | Run the **same verdict on dual-apply shadow runs** (DeepSeek/“Chinese” models) | The A/B comparison currently ranks shadow docs by the *deterministic* `ats_check.score` in the filename; both sides must be judged by the same independent LLM on their rendered PDFs for a fair comparison |

**Non-goals:** no blocking behaviour (verdict stays informational); no change to the
A–K sync contract; no historical backfill of verdicts for rows generated before Phase 1
(they have no `ats_verdict` in content.json); no per-vacancy judge-model switching.

**Note on “Chinese models” as PRIMARY:** when the owner switches the primary profile via
`/llm deepseek-r1` / `/llm deepseek-v3`, the verdict already runs — it is pipeline-level and
profile-agnostic, and it always uses the Anthropic judge (`JUDGE_*`), never the generation
profile. Nothing to do there. This phase only adds the missing SHADOW coverage.

---

## 3. G1 — Verdict → Google Sheets (column N)

### 3.1 Sheet column layout (do not break it)

The Sheet has three "writer families" that must never collide:

| Range | Owner | Written by |
|-------|-------|-----------|
| A–K | main tracker sync | `gsheets_client.COLUMNS` push/pull (`gsheets_sync`) |
| L "Applied Date" | `hunter/sent_normalizer.py` | daily 00:20 + `/normalize` |
| M "Cost $" | `hunter/cost_writer.py` | `mirror_cost_cell_sync` (rides `mirror_new_row`) |
| **N "ATS Verdict"** | **new: `hunter/verdict_writer.py`** | **this phase** |

Follow the `cost_writer.py` pattern **exactly** — it exists precisely because extending
`COLUMNS` to A–M would overwrite column L on every push. Same reasoning applies to N.
Read `hunter/cost_writer.py` top-of-file docstring before coding.

### 3.2 DB: new column `ats_verdict`

File: `hunter/db.py`. There is a lazy-migration list of `(column, definition)` tuples
(around line 129, currently ending with `("cost_usd", "REAL")`) that issues
`ALTER TABLE applications ADD COLUMN ...` for missing columns. Append:

```python
("ats_verdict", "REAL"),   # independent PDF-verdict score (0-100), Phase 2
```

No schema-version bump needed — the migration loop is idempotent (checks PRAGMA table_info).
Verify by opening a scratch DB in a test and asserting the column exists.

### 3.3 Tracker setter: `set_ats_verdict(url, score)`

File: `hunter/tracker.py`. The verdict is computed AFTER the tracker row already exists
(the row is written by the `generate_docs` subprocess → `tracker_service.record_successful_apply`
→ `add_applied`, which runs in apply Step 7; the verdict runs in Step 7.7). So this is a
post-hoc UPDATE by URL, exactly like `set_drive_url` (find it in tracker.py and mirror its
shape — normalize the URL with `normalize_url`, update by `url_norm`, return bool):

```python
def set_ats_verdict(url: str, score: float) -> bool:
    """Stamp the independent PDF-verdict score on the row matching `url`.
    Returns True if a row was updated. Never raises (log + False)."""
```

Also mark the row `sheets_dirty`? **No** — the A–K push does not carry this column, and we
mirror the cell directly (3.5). Setting dirty would cause a pointless A–K re-push.

### 3.4 New module: `hunter/verdict_writer.py`

Copy the structure of `hunter/cost_writer.py` (same helpers, same defensive style):

- `VERDICT_COLUMN = "N"` (module constant, mirrors cost_writer's hardcoded `"M"`).
- `VERDICT_HEADER = "ATS Verdict"`.
- `_row_for(row_id) -> tuple[int | None, float | None]` — read `sheets_row` + `ats_verdict`
  from the DB for one row id.
- `mirror_verdict_cell_sync(service, sheet_id, row_id) -> bool` — write the single cell
  `N{sheets_row}`; no-op (False) when `sheets_row` is NULL (row never mirrored — e.g. Sheets
  token was down at apply time; the eventual `/gsheets_resync` + backfill covers it) or the
  verdict is NULL.
- `write_verdict_header_sync(service, sheet_id)` — set `N1` if empty (bootstrap path doesn't
  know about N; cost_writer does the same for M — call it from the same place
  `write_cost_header_sync` is called from).
- `backfill_all_verdicts_sync(service, sheet_id) -> int` — one batchUpdate pushing every
  row with a non-NULL `ats_verdict` + non-NULL `sheets_row` into column N. Needed after
  outages and for rows whose mirror failed.

### 3.5 gsheets_sync wiring

File: `hunter/gsheets_sync.py`.

1. Find where `mirror_new_row` calls `cost_writer.mirror_cost_cell_sync` (~line 140–162).
   Do **not** add verdict there — at `mirror_new_row` time the verdict doesn't exist yet
   (it is computed after generate_docs, and mirror_new_row fires from inside generate_docs'
   tracker write). Instead add a standalone entry point:

```python
def mirror_verdict_for_url(url: str) -> bool:
    """Best-effort: push the ats_verdict of the row matching `url` into Sheet
    column N. Called from the apply pipelines right after the verdict is
    computed and set_ats_verdict() stored it. Safe no-op when GSHEETS_ENABLED
    is false / service unavailable / row not mirrored yet."""
```

   Implementation: guard on `GSHEETS_ENABLED`, resolve the row id by `url_norm` (tracker
   helper — reuse whatever `set_drive_url` uses to find the row, or add a small
   `get_row_id_by_url(url)` to tracker.py), get service + sheet id the way the cost mirror
   does (`_get_service()`, `_sheet_id()`), call `verdict_writer.mirror_verdict_cell_sync`.
   Wrap everything in try/except — a Sheets failure must never fail an apply.

2. Wire `write_verdict_header_sync` next to the existing `write_cost_header_sync` call
   (bootstrap / ensure-header path), so a fresh spreadsheet gets the N header.

### 3.6 Pipeline call sites

**`hunter/apply_api.py`, Step 7.7** — inside the existing `if verdict is not None:` branch
(added by PR #112), after `content["ats_verdict"] = verdict`:

```python
try:
    from hunter.tracker import set_ats_verdict
    from hunter.gsheets_sync import mirror_verdict_for_url
    if url and url != PASTE_NO_URL_PLACEHOLDER:
        set_ats_verdict(url, float(verdict["score"]))
        mirror_verdict_for_url(url)
except Exception as _sheet_err:
    print(f"[apply_agent] Warning: verdict Sheets mirror failed (continuing): {_sheet_err}")
```

Paste-only flow (`PASTE_NO_URL_PLACEHOLDER`): there is no URL key to find the row —
`add_applied` still creates a row (blank URL). Acceptable gap for now: skip the mirror and
log. Do NOT try to match by company+title (ambiguous).

**`hunter/apply_cli.py`** — mirror of the above, inside its verdict block (the one that
persists `ats_verdict` into content.json), same guards. Note the CLI pipeline has `url`
in scope; use the same skip-on-paste guard.

### 3.7 Backfill entry point

Extend the existing `/gsheets_push_missing`-family command file `hunter/commands/gsheets.py`
with the verdict backfill, OR (cheaper) add it to the existing `scheduled_gsheets_resync`
path — pick ONE, do not build a new command unless the diff stays small. Recommended:
call `backfill_all_verdicts_sync` at the end of `gsheets_resync`'s happy path (it is
idempotent and one batchUpdate). Document the choice in CLAUDE.md.

### 3.8 Interactions to double-check (known sharp edges)

- **`_reconcile_deleted_rows` / `mark_orphans_expired`** — operate on A–K + `sheets_row`
  only; column N is untouched. No change needed, but run `tests/test_bootstrap_dedup.py`
  and the reconcile tests to be sure nothing asserts on column count.
- **`tools/dedup_sheet.py`** deletes whole sheet rows — row-level, so N moves with the row.
  BUT any cached `sheets_row` in the DB shifts after a deletion; that staleness already
  exists for cost/M and is healed by resync. Same behaviour is fine for N — the backfill
  (3.7) self-heals.
- **Sheets API quota** — one extra `values.update` per apply is negligible (same as cost).
  The backfill is one batchUpdate.
- **`gsheets_client.COLUMNS` must NOT change.** If you find yourself editing it, you took
  a wrong turn — re-read 3.1.

---

## 4. G2 — Verdict on dual-apply shadow runs

### 4.1 Current shadow flow (read `hunter/dual_apply.py` first)

`run_shadow(primary_folder)` → `_generate_shadow()`:

1. Re-reads the primary's `job_posting.txt` (no re-fetch).
2. `llm_profiles.set_override(shadow_profile)` forces the shadow model (e.g. `deepseek-v3`)
   for every `call_llm` in the pipeline building blocks.
3. `call_llm` (generation) → `_ats_check_loop` → scrubs → lang gate →
   `generate_docs --no-tracker` into `{Company}/{shadow_name}/`.
4. `_ats_suffix(content)` builds `"_ats88"` from **`content["ats_check"]["score"]`**
   (the deterministic combined score) and `_suffix_docs(sub, suffix)` renames the rendered
   CV/CL files (see `_SKIP_SUFFIX_NAMES` for exclusions).
5. `gdrive_sync.upload_shadow_folder(...)` uploads the shadow subfolder (best-effort).

Deterministic-loop savings from Phase 1 **already apply** here (same `_ats_check_loop`),
so the shadow got cheaper automatically. What's missing is the independent verdict.

### 4.2 Changes

All in `hunter/dual_apply.py::_generate_shadow`, between generate_docs success and the
filename suffixing (i.e. right before the current `_ats_suffix` call):

```python
# Independent PDF verdict — same judge as the primary (JUDGE_* config), so the
# A/B filenames compare like-for-like. set_override() does NOT affect this call:
# run_llm_verdict reads hunter.config.JUDGE_* directly, never llm_profiles.
try:
    from hunter.ats_pdf_roundtrip import run_llm_verdict
    verdict = run_llm_verdict(folder=sub, job_text=job_text)
    if verdict is not None:
        content["ats_verdict"] = verdict
        (sub / "content.json").write_text(
            json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"[dual] shadow verdict: {verdict['score']}%")
except Exception as e:
    print(f"[dual] shadow verdict failed (continuing): {e}")
```

Then update `_ats_suffix(content)` to **prefer the verdict**:

```python
def _ats_suffix(content: dict) -> str:
    """'_ats88' — prefers the independent PDF verdict; falls back to the
    deterministic ats_check score; '' if neither is available."""
    for path in (("ats_verdict", "score"), ("ats_check", "score")):
        try:
            score = content.get(path[0], {}).get(path[1])
            if score is not None:
                return f"_ats{round(float(score))}"
        except (TypeError, ValueError):
            continue
    return ""
```

Ordering constraints (why "between generate_docs and suffixing"):

- `run_llm_verdict` locates the PDF via `find_en_cv_pdf(sub)` with patterns like
  `*CV*EN*.pdf` — the files are still UNsuffixed at that point, patterns match. After
  suffixing (`..._EN_ats88.pdf`) they still match, but running before keeps it simple and
  the suffix can then carry the verdict.
- `upload_shadow_folder` runs after suffixing → the renamed files AND the updated
  `content.json` (with `ats_verdict`) land on Drive with no extra work.

### 4.3 Shadow constraints that MUST hold

- **No tracker row, no Telegram, no Sheets** for the shadow — do NOT call
  `set_ats_verdict` / `mirror_verdict_for_url` from dual_apply. The verdict lives only in
  the shadow's `content.json` + filename + Drive copy. (The primary's Sheets cell N always
  refers to the primary/boevoy document.)
- **Judge key in the detached process** — the shadow runs detached
  (`dual_apply.launch_detached` → `python -m hunter.dual_apply` with a
  `DUAL_SHADOW_TIMEOUT_SEC` watchdog). It inherits the parent env, so `JUDGE_API_KEY` /
  `ANTHROPIC_API_KEY` are present. No change needed; just be aware when testing.
- **Timing budget** — the verdict adds one Haiku call (~2–5 s) inside the watchdog window
  (default 900 s). Negligible; no config change.
- **Best-effort** — any verdict failure logs and continues; the shadow (and a fortiori the
  primary) must never fail because of it.

---

## 5. Tests (write per milestone, keep suite green)

Existing suites to extend (match their style):
`tests/test_ats_pdf_roundtrip.py` (verdict unit tests exist after #112),
`tests/test_dual_apply.py`, `tests/test_gsheets_sync.py` (or nearest gsheets test file),
`tests/test_tracker*.py`.

Minimum new coverage:

1. **DB migration** — fresh scratch DB has `ats_verdict` column (PRAGMA table_info).
2. **`set_ats_verdict`** — updates the row matched by normalized URL; returns False on
   unknown URL; never raises.
3. **`verdict_writer.mirror_verdict_cell_sync`** — writes `N{row}` with mocked service;
   no-op when `sheets_row` is NULL; no-op when verdict is NULL.
4. **`write_verdict_header_sync`** — sets N1 only when empty (mock existing header).
5. **`backfill_all_verdicts_sync`** — batches only rows with both verdict + sheets_row.
6. **`mirror_verdict_for_url`** — no-op when GSHEETS_ENABLED false (monkeypatch config);
   happy path calls the cell mirror with the resolved row id.
7. **apply_api Step 7.7 wiring** — with a mocked verdict, `set_ats_verdict` +
   `mirror_verdict_for_url` are called for URL flow and NOT called for paste flow.
8. **dual_apply** — `_generate_shadow` stores `ats_verdict` in the shadow content.json
   (mock `run_llm_verdict`); `_ats_suffix` prefers verdict over ats_check and falls back
   correctly; verdict failure doesn't break the shadow (existing failure-path test style).
9. **A–K contract untouched** — assert `gsheets_client.COLUMNS` still ends at K (cheap
   regression guard; cost_writer tests may already have an analogue for M — mirror it).

Run: `python -m pytest tests/ -q` (full suite — must stay green) and `python -m ruff check .`.

---

## 6. Verification (live, after code review)

1. `python tools/preview_apply.py` won't exercise Sheets — do a real run instead:
   in Telegram, `/force <real vacancy URL>` on the dev instance with `GSHEETS_ENABLED=true`.
2. Confirm: Telegram message shows `ATS: NN% (independent, PDF)`; the Sheet row's column N
   holds the same number; `tracker.db` row has `ats_verdict` set
   (`sqlite3 tracker.db "SELECT ats_verdict FROM applications ORDER BY rowid DESC LIMIT 1"`).
3. `/dual on`, run another `/force`, wait for the detached shadow: shadow subfolder
   contains `content.json` with `ats_verdict`, doc filenames end `_ats{NN}.pdf` where NN is
   the VERDICT score, Drive copy shows the same files. `/dual off` afterwards.
4. Delete nothing; leave the test rows for the owner to inspect.

---

## 7. Milestones / commit plan (one commit each, tests included)

| M | Scope | Files |
|---|-------|-------|
| M1 | DB column + `set_ats_verdict` (+ tests 1–2) | `hunter/db.py`, `hunter/tracker.py` |
| M2 | `verdict_writer.py` + gsheets_sync wiring + header + backfill (+ tests 3–6, 9) | `hunter/verdict_writer.py`, `hunter/gsheets_sync.py` |
| M3 | Pipeline call sites API + CLI (+ test 7) | `hunter/apply_api.py`, `hunter/apply_cli.py` |
| M4 | Shadow verdict + suffix preference (+ test 8) | `hunter/dual_apply.py` |
| M5 | Docs: CLAUDE.md (tracker schema table — new DB column; Sheets workflow section — column N; config table cross-ref; Agent Work Log entry) | `CLAUDE.md` |

Rules from CLAUDE.md apply: `python -m compileall .` after edits, ruff green, full pytest
green, never commit `.env`/tokens/tracker files, update CLAUDE.md in the same PR.
Open ONE PR to `master` titled `feat(ats): verdict to Google Sheets + dual-apply shadow verdict`.

---

## 8. Edge cases & FAQ for the implementing agent

- **Sheets token dead at apply time** → `mirror_verdict_for_url` no-ops (row has no
  `sheets_row` or service fails); the resync-time backfill (3.7) heals it later. Never retry
  inline.
- **Verdict is None** (no judge key, PDF unreadable, LLM error) → nothing is stored, cell N
  stays empty, filename falls back to the deterministic score. All downstream code must
  tolerate NULL/absent `ats_verdict`.
- **Why column N and not overwriting the A–K "ATS %" column?** The ATS % column (tracker
  col 5) is written at row-creation time from the generator's self-score and participates
  in the A–K conflict matrix; overwriting it post-hoc would fight the pull logic. A
  dedicated, bot-owned column has no conflict surface — proven pattern (L, M).
- **Should the shadow verdict use the shadow's own model as judge?** No. The whole point of
  the A/B comparison is a COMMON yardstick: both primary and shadow are judged by the same
  Anthropic judge model on their rendered PDFs.
- **Rounding** — store the raw float in DB/JSON; round only for display (filename suffix
  and Telegram already round).
