# Frozen eval corpus — plan

**Status:** draft. M0 not run. No code yet.
**Date:** 2026-09-27
**Owner ask (2026-09-27):** keep a ~10 MB set of real vacancies and re-run it whenever prompts or filters change.
**Decision:** grill session Q17 in [USER_SETTINGS_MODEL_PLAN](USER_SETTINGS_MODEL_PLAN.md). The corpus lives in a **private** repo; this repo holds only a manifest.
**First consumer:** USER_SETTINGS_MODEL_PLAN M0.b, the owner-equivalence replay.

## Why

Every eval tool in the repo reads a corpus that moves or is too small.

**`tools/eval_golden.py`** (prompt A/B) stores relative paths and sha256 hashes into `Applications/**` on the deploy host. The folders get pruned and re-generated. A golden entry whose hash no longer matches is a lost data point, and the set can't be replayed off the VPS.

**`tests/fixtures/filter_parity/`** (the FILTERS_YAML M1 parity freeze) is public, so it replays in CI. But it covers only the handful of hand-written `tests/fixtures/sample_jobs/` postings.

**The calibration tools** (`tools/screen_calibrate.py`, `tools/prescreen_calibrate.py`, `tools/reuse_calibrate.py`) read whatever `Applications/` holds that day. Two runs a month apart are not comparable.

**Nothing replays FILTERED-OUT listings at all.** `Applications/` only holds postings that already passed the listing filters, so a filter change that newly lets junk through can't be seen on it.

The repo is public, so the text itself can't live here. Postings carry recruiter names, e-mails and phones, and they are third-party text.

Checked before writing:
- `docs/AGENT_LOG.md` has no rejected version.
- `docs/improvement-2026-09/08-DATA_EVAL_PLAN.md` M2 built `eval_golden.py` on purpose without text. That stays right for this repo; this plan adds a private home for the text rather than reversing it.

## What the corpus holds (version = a git tag in the private repo)

**Part L — listings.** One JSON line per listing: `url_norm`, source, title, company, location, salary raw, the non-empty `job.raw` KEYS+values the filters read, and the live verdict at freeze time.
- Where it comes from: `postings_seen` gives the metadata. Its `filter_verdict` covers BOTH passed and rejected listings, which is exactly what the listing-filter replay needs.
- `postings_seen` drops `job.raw`, so the freeze adds a live sweep dump from `tools/market_memory_m0.py --dump`, which keeps the raw keys that `_is_react_without_angular` reads.

**Part P — postings.** `job_posting.txt` from `Applications/**`, plus the tracker row's outcome: applied / SKIP reason / EXPIRED / sent / outcome label.
- It includes **every** posting with a HARD doomed-gate verdict and **every** posting the owner really sent.
- The rest is a stratified sample by source × posting language × detected stack.

**Manifest in this repo:** `tests/fixtures/eval_corpus/manifest.json`. It records:
- the corpus version (tag);
- per item: id, part, source, lang, stack, sha256, and the verdicts expected at freeze time.

No text is stored here. The manifest is what makes a replay reproducible, and it is safe to be public.

**Size target:** ~10 MB (owner). M0 decides whether that is "everything" or a sample.

## M0 — free, read-only measurement on the VPS (decision rules stated first)

`tools/corpus_m0.py`, run on the VPS, reports:
- the count and total bytes of `Applications/**/job_posting.txt`, shadow subfolders excluded;
- the split by source / lang / stack;
- the count of HARD-verdict postings;
- the count of sent postings;
- `postings_seen` rows split by verdict;
- the bytes of a live `--dump`.

Decision rules:
- **Parts P+L ≤ 15 MB** → freeze everything; no sampling bias.
- **> 15 MB** → keep all HARD and all sent postings, then sample the rest stratified down to ~10 MB. The sampling seed is recorded in the manifest.
- **Fewer than 300 postings in any stack stratum that M0.b needs** (today: angular, react, fullstack) → M0.b reports that stratum as UNMEASURED instead of green.

## Milestones

**M1 — Freeze.** `tools/corpus_freeze.py`, VPS only. It:
1. builds Parts L and P;
2. writes them into a local checkout of the private repo `igrdevelop/job-hunter-corpus`;
3. commits and tags `v<date>`;
4. writes the manifest into this repo's working tree for a normal PR.

It never overwrites a tag. A new version is always an explicit new freeze.

**M2 — Fetch.** `tools/corpus_fetch.py` clones or pulls the tagged version into a gitignored `.eval_corpus/`, then verifies every sha256 against the manifest, failing on any mismatch. The token comes from `EVAL_CORPUS_TOKEN`, or from the local git credentials.

**M3 — Replay.**
- `tools/corpus_replay.py filters` replays `classify_job` / `assess_job_text` / the stack gates over Parts L+P, with an optional `--profile filters.yaml`. It diffs the result against the manifest's frozen verdicts. The exit code is non-zero on any pass/skip flip. This is USER_SETTINGS_MODEL_PLAN M0.b.
- `tools/corpus_replay.py generate` feeds Part P to `tools/eval_golden.py score` as the golden set. The paid LLM paths keep `eval_golden`'s existing `--yes` gate.

**M4 — CI.** A `corpus` job runs `corpus_replay.py filters`, but only when the `EVAL_CORPUS_TOKEN` secret exists (the same skip-if-no-secret pattern as the Sonar job). Without the secret, the public `filter_parity` fixtures remain the gate. The job does not block deploy until it has been green for two weeks.

## Non-goals

- De-identification. The repo is private, and cutting contacts could change gate verdicts on exactly the text the gates read.
- Automatic re-freezing on a schedule. A corpus that moves on its own is the problem this plan exists to fix.
- Storing generated `content.json` / PDFs. Generation outputs are produced per eval run, not frozen.

## Risks

- **Private-repo access** on a new dev machine or in CI needs a token. Without it, the replay degrades to UNMEASURED, never to a false green.
- **Stale corpus.** Boards change their markup and the market changes its stacks. The manifest's freeze date is printed on every replay, so a year-old corpus is visible. A re-freeze is a deliberate PR.
