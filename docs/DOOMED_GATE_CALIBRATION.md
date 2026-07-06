# Doomed-gate calibration report (M4)

Companion to [`DOOMED_GATE_PLAN.md`](DOOMED_GATE_PLAN.md) — the M4 acceptance
requirement ("проверь на реальных линках из таблицы в гугл шитах") run via
`tools/screen_calibrate.py`.

## How it was run

```
python tools/screen_calibrate.py --live
```

Two data sources, combined:

1. **Offline corpus** — every `Applications/**/job_posting.txt` on this
   machine (`Applications/` + the `Applications_DeepSeek_R1` /
   `Applications_DeepSeek_V3` comparison runs), 375 files.
2. **Live spot-check** — 20 non-LinkedIn URLs sampled from the Google Sheets
   tracker with a real `Sent` date in the last 45 days, fetched fresh via
   `hunter.sources.fetch_job_text` with a 2.5s pause between requests
   (19 fetched OK, 1 dead link (`Hospitable`, 404), 12 of the 19 now report
   as expired on re-fetch — expected, most are 1–2 months old).

Total: **394 postings scanned** against `hunter.filters.assess_job_text`.

## Final result

```
Findings: 38 hard, 52 soft
Clean postings (no finding): 320

HARD findings on rows the owner actually SENT (must be zero): 0
```

**Acceptance bar met: 0 HARD false positives on rows the owner actually
sent.** The 4 remaining HARD hits on Sent rows are `is_ai_training_or_mill`
on Micro1 (13 May / 19 Jun sends) — that rule is an exact-name lookup
against the owner-curated `exclude_companies` list added in PR #110
(2026-06-30), i.e. a deliberate policy decision made *after* those Sent
dates. There is no regex to narrow; the row is stale ground truth, not a
gate precision problem. `screen_calibrate.py` reports these separately
(`pre-policy`) and excludes them from the false-positive count, the same way
it already excluded re-fetched-as-expired ("stale") hits.

- `BigbearAI` (hybrid in McLean, Virginia — the case that started this plan)
  and `Megaport` (Vue 3/Nuxt stack mismatch) are not present in this
  machine's corpus/live sample, so the calibration script can't exercise
  them directly; both are covered by dedicated regression fixtures instead
  (`tests/test_doomed_gate.py::test_bigbearai_caught_by_foreign_onsite_hard_rule`,
  `::test_megaport_caught_by_stack_mismatch_soft_rule`, fixtures under
  `tests/fixtures/doomed_gate/`).

## False positives found and fixed during calibration

Three real false positives surfaced on the first calibration pass (6 hard
hits on Sent rows, one narrowed away in a `docs`-comment iteration + one
found only via `--live`); all three fixed in `hunter/filters.py` before this
report:

1. **`foreign_onsite_hybrid` — BitPanda perks bullet.** A real SENT (and
   subsequently relocated-for) posting listed a benefits bullet — *"Fuel and
   focus on-site – Pandas in Vienna, Bucharest, Barcelona, and Berlin can
   enjoy free onsite dining"* — that tripped the HARD foreign-onsite rule
   purely because four foreign cities sat within the 120-char proximity
   window of the word "onsite". Fixed with `_onsite_signal_positions()`: an
   "on-site"/"onsite" occurrence immediately followed by a perks/benefits
   word (dining, lunch, snacks, gym, cafeteria, coffee, parking, …) is
   dropped before the city-proximity check runs. Shared by both the new HARD
   foreign-location rule and the existing SOFT PL anti-hybrid-city check.

2. **`is_german_language_required` — DHCBusinessSolutions "Nice to have".**
   A real SENT theprotocol.it posting listed *"Nice to have — Optional, 5+
   years of commercial experience, German language skills, Usage of Nx"* —
   explicitly optional, not a requirement. Fixed with an
   `_is_optional_context()` veto: a German-required match preceded (within
   150 chars) by an explicit "nice to have"/"optional"/"bonus"/"a plus"/
   "mile widziane"/"dodatkowym atutem" marker is not treated as a hard
   requirement. A genuine requirement stated elsewhere in the same posting
   still fires (regression test:
   `test_german_still_hard_when_actually_required_near_nice_to_have_section`).

3. **`is_ai_training_or_mill` — Micro1 pre-policy Sent rows.** Not a code
   fix — see "Final result" above; documented as an accepted exception in
   `screen_calibrate.py` instead of loosened, since loosening it would
   silently undo the owner's PR #110 decision.

Regression tests for (1) and (2) live in `tests/test_doomed_gate.py`
(`test_foreign_onsite_hybrid_vetoed_by_perks_bullet`,
`test_foreign_onsite_hybrid_not_vetoed_by_unrelated_perks_word_elsewhere`,
`test_german_vetoed_when_listed_as_nice_to_have`,
`test_german_still_hard_when_actually_required_near_nice_to_have_section`).

## Soft findings — spot-checked, no changes needed

52 SOFT findings (mostly `is_unwanted_onsite_location` on genuinely
hybrid/on-site Polish postings the owner still chose to send, and
`stack_mismatch_non_candidate_framework` on Vue/Svelte postings that
mention Angular/React only in passing or not at all). SOFT never blocks
generation — it only adds a Telegram warning — so these are expected and
require no action; they're the reason `has_body_disqualifier` and
`is_unwanted_onsite_location` were downgraded from HARD to SOFT during
earlier iterations of this same milestone (see the comment above
`_MANUAL_SCREEN_CHECKS_HARD` in `hunter/filters.py`).

## Reproducing this report

```
python tools/screen_calibrate.py            # offline corpus only, no network
python tools/screen_calibrate.py --live      # + 20-URL live Sheet spot-check
```

Both are read-only / dry-run by default — no writes to the Sheet, the local
tracker, or any `job_posting.txt`.
