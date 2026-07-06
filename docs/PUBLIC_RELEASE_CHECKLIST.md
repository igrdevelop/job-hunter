# Public Release Checklist

Steps to flip `igrdevelop/job-hunter` from private to public. Items 1–2 are
already done on the branch; **item 3 (history scrub) is manual and destructive
— read it fully before running anything.**

## 1. Working tree is clean of personal data ✅ (done)

- `prompts/candidate_profile.md`, `prompts/base_cv_*.md`, `prompts/candidate/`,
  `prompts/examples/` — untracked + gitignored; `.example` templates +
  `prompts/README.md` ship instead.
- `.mcp.json` (local machine paths) — untracked + gitignored.
- `docker-compose.yml` mounts the personal files from the host (they are no
  longer inside the image).
- Secrets were never tracked (verified: no `.env`, tokens, tracker data in
  `git ls-files`).

## 2. Repo presentation ✅ (done)

- `README.md`, `LICENSE` (MIT), decluttered root.

## 3. History scrub (manual, one-time, DESTRUCTIVE)

The personal files above still exist in **every old commit**. Removing them
from history rewrites all commit hashes and requires a force-push.

### 3.1 Preconditions

- [ ] Merge all open PRs / branches first (rewritten history orphans them).
- [ ] Fresh full backup: `git clone --mirror https://github.com/igrdevelop/job-hunter.git job-hunter-backup.git`
- [ ] `pip install git-filter-repo`

### 3.2 Scrub

Run in a **fresh clone** (filter-repo refuses to run in a dirty working repo):

```bash
git clone https://github.com/igrdevelop/job-hunter.git scrub && cd scrub

git filter-repo \
  --invert-paths \
  --path prompts/candidate_profile.md \
  --path prompts/base_cv_angular.md \
  --path prompts/base_cv_react.md \
  --path prompts/base_cv_ai.md \
  --path prompts/base_cv_fullstack_angular_nest.md \
  --path prompts/base_cv_fullstack_react_next.md \
  --path prompts/candidate \
  --path prompts/examples \
  --path .mcp.json
```

Also worth checking for historical paths that may have carried personal data
before renames (search then add `--path` entries as needed):

```bash
git log --all --diff-filter=A --name-only --format= | sort -u | grep -iE 'cv|profile|candidate|about_me|cover'
```

### 3.3 Verify

```bash
git log --all --full-history -- prompts/candidate_profile.md   # must be empty
git grep -i "petrasheuski" $(git rev-list --all) | head        # must be empty (slow; Ctrl+C after silence)
```

### 3.4 Force-push and re-sync

```bash
git remote add origin https://github.com/igrdevelop/job-hunter.git
git push origin --force --all
git push origin --force --tags
```

- Every existing clone (dev machine, worktrees) must be re-cloned — old
  clones would push the old history back.
- GitHub may keep orphaned commits reachable by SHA in caches/PR views.
  For a private→public flip this is acceptable risk after the scrub, but the
  bulletproof option is: create a fresh repo and push the scrubbed history
  there, then delete the old repo (loses stars/issues/PR history).

## 4. GitHub settings after flipping public

- [ ] Description: "Autonomous job-hunting bot: 21 scrapers, LLM-tailored CVs with a 7-layer anti-hallucination pipeline, Telegram control, Google Sheets/Drive tracking"
- [ ] Topics: `python`, `telegram-bot`, `llm`, `anthropic`, `job-search`, `automation`, `web-scraping`
- [ ] Social preview image (Settings → General)
- [ ] Check the Actions tab is intended to be public (CI logs become visible;
  the deploy job uses secrets — values are masked, but review the log output once)
- [ ] Tag a release: `git tag v1.0.0 && git push origin v1.0.0` + auto-generated release notes

## 5. Deploy host follow-up (after the compose change)

On the VPS, next to `docker-compose.yml`, create `./prompts/` and copy the real
personal files into it (`candidate_profile.md`, `base_cv_*.md`, `examples/`)
**before** the next `docker compose up` — the image no longer contains them.
