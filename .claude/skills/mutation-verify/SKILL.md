---
name: mutation-verify
description: Prove that a test actually guards the code it claims to guard. Applies a minimal mutation to the production line the test protects, runs only that test, confirms it FAILS for the right reason, then restores the file and confirms green again. Use after writing a regression test, before claiming a fix is covered, or when auditing an existing test you suspect passes with or without the fix.
---

You are verifying that a test has teeth.

**Target:** $ARGUMENTS — a test file, a `file::test_name`, or a description of
the fix whose coverage is in question.

## Why this exists

A test that passes both with and without the fix is worse than no test: it
reports safety that does not exist. On a suite of ~2400 tests you cannot spot
those by reading. The only reliable check is to break the production code on
purpose and watch whether the test notices.

This is already the standard in this repo — several work-log entries say
"mutation-verified" (the golden E2E test was validated against three hand
mutations; the Drive-race tests against reverting the lock). This skill just
makes it repeatable.

## Step 0 — Safety preconditions

1. `git status --porcelain` — if the production file you are about to mutate has
   **uncommitted changes**, STOP and tell the human. Restoring via
   `git checkout` would destroy their work. Offer to proceed only after they
   commit or stash.
2. Record the exact restore command before mutating anything:
   `git checkout -- <path>` (or `git stash pop` if you stashed).
3. You mutate **production code only**. Never mutate the test — a test edited
   to fail proves nothing.

## Step 1 — Find what the test guards

Read the test. Identify the specific production behavior it asserts, and locate
the line(s) in `hunter/` (or `llm_client.py` / `generate_docs.py` /
`apply_agent.py`) that implement it. Name the file and line before touching it.

If the test asserts several independent behaviors, pick them one at a time —
one mutation per run, or the result is unattributable.

## Step 2 — Choose a minimal mutation

Smallest change that removes the guarded behavior. In rough order of preference:

- comment out the line that does the work (a tracker stamp, a scrub call, a
  gate check)
- invert a boolean condition (`if x:` → `if not x:`)
- neutralise a threshold (`>= 0.97` → `>= 0.0`)
- return early / return the unmodified input from a transform
- drop an argument that carries the behavior (`--no-tracker`, `reapplication=True`)

Do **not** delete whole functions or break imports — a test that fails with
`ImportError` has proven nothing about the behavior. The mutation must leave the
code runnable.

## Step 3 — Run only the target test

```bash
python -m pytest "<file>::<test>" -q --no-header
```

Only that test. A full-suite run is slow and buries the signal.

## Step 4 — Judge the result

**The test failed** — now check *how*. The failure message must relate to the
guarded behavior (a wrong value, a missing call, a wrong count). If it failed
with `ImportError`, `AttributeError`, `SyntaxError` or a fixture error, the
mutation was too crude: restore, pick a smaller one, run again.

**The test passed** — this is the finding. The test does not cover the fix.
Record it, restore, and report; do not silently "fix" the test unless the human
asks. Naming the gap is the deliverable.

## Step 5 — Restore and confirm

```bash
git checkout -- <path>
python -m pytest "<file>::<test>" -q --no-header
```

Green again. **This step is not optional** — run it even if an earlier step
errored out. Leaving a mutation in the working tree is the one way this skill
can do damage.

Then confirm the tree is clean: `git status --porcelain`.

## Step 6 — Report

| Мутация | Файл:строка | Ожидали | Факт | Вывод |
|---|---|---|---|---|
| Закомментирован `set_ats_verdict` | hunter/apply_api.py:412 | падение | упал: `assert None == 91` | тест держит |
| Инвертировано `if sim >= 0.97` | hunter/repost_gate.py:88 | падение | ПРОШЁЛ | **тест не покрывает порог** |

Close with the restore confirmation (`рабочее дерево чистое`) and, if any
mutation survived, one sentence on what assertion is missing.

If the work is significant, the phrase to use in the Agent Work Log is
"mutation-verified" plus the count — that is the existing convention here.
