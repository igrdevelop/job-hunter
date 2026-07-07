# Doomed-gate paste-path extension — calibration report

Companion to [`DOOMED_GATE_PASTE_PLAN.md`](DOOMED_GATE_PASTE_PLAN.md). Same
acceptance bar as the original M4 (`DOOMED_GATE_CALIBRATION.md`): **0 HARD
findings on rows the owner actually Sent** (excluding stale/re-fetch-expired
and pre-existing-policy rows, both tracked separately by
`tools/screen_calibrate.py`).

## What changed since M4

1. **`title_exclude_pattern` (HARD)** — reuses the listing-level
   `_matches_exclude_pattern` (Java/.NET/C#/PHP/Vue/Magento/…) against the
   known or guessed title. Catches Santander `.NET Developer (Angular)`.
2. **`off_domain_title` (SOFT)** — reuses `_matches_title_keywords` (inverted)
   against the known or guessed title. Catches QuantumBlackMcKinsey
   `Software Engineer - QuantumBlack, AI by McKinsey`.
3. **`_guess_title_from_text`** — best-effort first-meaningful-line title
   guess, used only when no explicit title is passed to `assess_job_text`.
4. **`hunter/services/apply_service.py`** — `--company`/`--title` are now
   passed to the apply subprocess for **every** auto-hunt job with a known
   title, not just `jobleads.com` ones. This fixed a real, unrelated bug:
   `is_unwanted_fullstack` and (now) `title_exclude_pattern` are
   title-dependent rules that had **never fired for any non-JobLeads job**,
   because the doomed gate never actually received a title for them before.
5. **Dropped, not shipped:** an earlier `header_location_anti_hybrid_city`
   SOFT rule (bare anti-hybrid city mention near the top of the text, no
   onsite/hybrid wording required) was implemented to catch Comarch
   (`гибрид не вроцлав`), then immediately reverted after calibration showed
   it also fired on **Fairmarkit** — a real, fully described, Sent (98% ATS)
   Warsaw-office EU role with no hybrid language of its own. A bare city
   mention can't be told apart from "this is just where the office is";
   Comarch-style cases are not solvable from the fetched text without
   fabricating a signal that isn't there.

## Result

Offline corpus (375 files, `Applications/` + both DeepSeek comparison runs)
+ live 20-URL Google Sheet spot-check, run with real Sheet titles fed into
the offline replay (`_title_index`, matching what `apply_service.py` now
does in production):

```
Postings scanned: 394
Findings: 86 hard, 116 soft
Clean postings (no finding): 265

HARD findings on rows the owner actually SENT (must be zero): 0
```

7 additional HARD hits were excluded as pre-existing policy, not new
imprecision (`_PRE_EXISTING_POLICY_RULES` in `screen_calibrate.py`):

- **Micro1 ×4** — `is_ai_training_or_mill`, an exact-name blocklist lookup
  (PR #110, 2026-06-30); these Sent dates (13 May / 19 Jun) predate that
  decision — same story as the original M4 report.
- **BCFSoftware** — `title_exclude_pattern` on `Senior Angular Developer
  (Tech Lead) M/K` (matches the existing `\btech\s+lead\b` exclude pattern).
- **Unide ×2** — `is_unwanted_fullstack` on `Senior Fullstack Developer
  (Node)` (no Angular in title — matches the existing fullstack policy
  exactly).

The last three are the EXISTING, already-owner-approved policy firing for
the first time on old rows that only got through because of the
`apply_service.py` title-plumbing bug (item 4 above) — not something to
"fix" by loosening a pattern. New findings from these rules on FUTURE
applies should still be scrutinized normally.

**Rebase note (2026-07-07):** this branch was rebased onto `origin/master`
after PR #118 (`fix(gate): mill name in job BODY -> HARD skip`) merged,
which added a new `ai_mill_body` rule — the same `exclude_companies`
blocklist re-checked against the full body text. It fires on the same
pre-Sent Micro1 rows for the same reason, so `ai_mill_body` was added to
`_PRE_EXISTING_POLICY_RULES` alongside `is_ai_training_or_mill`. Final
post-rebase run: still 0 HARD false positives (91 hard / 116 soft findings,
11 pre-policy-excluded), 1763 tests green, ruff clean.

## A note on the guessed-title SOFT noise

The raw offline-corpus run (before feeding it real Sheet titles) showed a
much noisier number — 186 SOFT findings, many `off_domain_title (guessed)`
hits on garbage like `"Company (from listing): X"` or `"URL: (none — pasted
by user)"`. This is a **calibration-harness artifact, not a production
concern**: `job_posting.txt` files are saved with a `"URL: {url}\n\n"` (and,
for the JobLeads-blocked stub, a `"Company (from listing): ...\nTitle (from
listing): ...\n\n"`) header **after** the doomed gate already ran on the raw
`job_text` — the archived file's first line is never what
`_guess_title_from_text` would see in production. Feeding the offline replay
real Sheet titles (`_title_index`) sidesteps this for every row with a Sheet
match; the guess path itself is only ever exercised in production by a
genuine manual paste with no `Job` object at all (no title anywhere), which
the live spot-check's 20 URLs — all of which had a Sheet-known title — don't
happen to exercise either. The guess heuristic is unit-tested directly
(`test_guess_title_from_text_skips_boilerplate_lines`,
`test_title_exclude_pattern_hard_with_guessed_title`,
`test_off_domain_title_soft_with_guessed_title`) against realistic raw-text
shapes instead.

## Reproducing this report

```
python tools/screen_calibrate.py --live
```
