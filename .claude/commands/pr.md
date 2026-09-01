---
description: Open a pull request with this repo's pre-flight checks — branch cut from current origin/master, ruff check + format, pytest, project-invariants-review, a code-review pass on the diff, English-only body with no attribution lines.
argument-hint: [PR title hint or plan-doc name]
---

Open a pull request for the current work, with this repository's pre-flight checks.

## Input
$ARGUMENTS — optional PR title hint, or a plan-doc name to link. If empty, derive both from the commits.

---

## Step 1 - Branch hygiene (the one that has burned us before)

```bash
git fetch origin --quiet
git rev-parse --abbrev-ref HEAD
git merge-base --is-ancestor origin/master HEAD && echo "BASE OK" || echo "BASE STALE"
```

Rules:
- **Never open a PR from `master`.** If HEAD is `master`, stop and create a branch first.
- If the base is STALE — the branch was cut from an outdated `origin/master` — do **not** rebase. Create a fresh branch off current `origin/master` and re-apply the work there. One branch per PR.
- If the branch already has an open PR, this is an update, not a new PR — say so and stop.

---

## Step 2 - Gates

Run all three, in this order, and stop at the first failure:

```bash
ruff check .
ruff format --check .
python -m pytest tests/ -q --no-header
```

`ruff format --check` is a CI gate — a formatting diff fails the build, so fix it with `ruff format .` and amend rather than pushing and waiting.

If a hook or a compile error shows up, `python -m compileall .` narrows it down.

---

## Step 3 - Invariant pre-flight

Run the `project-invariants-review` agent against the branch diff.

Its findings are advisory except for two, which are hard stops:
- a protected file staged (`.env`, `tracker.xlsx`, `Applications/`, tokens)
- `requirements.lock` out of sync with `pyproject.toml`

Report the rest to the human and let them decide whether to fix now or file follow-up.

---

## Step 4 - Code review

Run the `code-review` skill on the branch diff at **medium** effort. Skip only
if the human explicitly said to, or if this exact diff was already reviewed in
this session — and say so in the report either way; never imply it ran when it
did not.

- **CONFIRMED correctness findings are a hard stop:** fix them (or get an
  explicit "ship anyway") before opening the PR.
- PLAUSIBLE and quality findings are advisory — list them in the report; fix
  the cheap, obvious ones, file the rest as follow-ups.
- After fixing a finding, verify that specific fix (targeted test / re-read);
  don't re-run the whole review.

This is the pre-publication pass. CodeRabbit (`.coderabbit.yaml`) reviews the
PR *after* it opens — this step is what catches problems while they are still
private.

---

## Step 5 - Compose the PR

Language: **English only** — title and body. The repo is public. Quoted data (a Russian regex being added, an owner report, a bot UI string) may stay in its original language; the surrounding prose may not.

Body structure:

```
## What

<2-4 sentences: the problem, then the change. Lead with the user-visible effect,
not the file list.>

## Why

<the incident, measurement or plan doc that motivated it. Link docs/<NAME>_PLAN.md
if one exists.>

## Testing

<what was added, and how it was verified. If a test was mutation-verified, say so
and give the count.>

## Notes

<config changes, migrations, ops steps needed after deploy — or "none">
```

Do **not** add `Co-Authored-By` lines. Do not add a "Generated with" footer unless the human asks.

---

## Step 6 - Push and open

```bash
git push -u origin HEAD
gh pr create --base master --title "<title>" --body "<body>"
```

Print the PR URL as a markdown link when done.

---

## Step 7 - Report

One short summary: branch, gates (pass/fail), invariant findings count, code-review findings count (fixed / follow-up), PR link. If anything was skipped, say which and why — never imply a gate ran when it did not.
