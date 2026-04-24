---
name: release-notes
description: Generate a structured changelog from git commits between develop and master branches, then append it to DEPLOY.md. Use before merging develop into master.
disable-model-invocation: true
---

Generate release notes for the next merge from develop to master.

## Steps

1. **Get the commit list**
   Run: `git log master..develop --oneline --no-merges`
   If there are no commits, report "Nothing to release — develop is up to date with master."

2. **Group commits by type**
   Use conventional commit prefixes if present, otherwise infer from message:
   - `fix:` / bug / error / broken → **Fixes**
   - `feat:` / add / new / implement → **Features**
   - `refactor:` / cleanup / reorganize → **Refactoring**
   - `chore:` / deps / update / bump → **Chores**
   - `test:` → **Tests**
   - `docs:` → **Docs**

3. **Draft the changelog section**
   Format:
   ```
   ## Release YYYY-MM-DD

   ### Features
   - ...

   ### Fixes
   - ...

   ### Refactoring
   - ...
   ```
   Use present tense, imperative mood. Keep each line concise (under 80 chars).
   Skip empty sections.

4. **Show the draft to the user** and ask for confirmation before writing.

5. **On confirmation** — prepend the new section after the first line of DEPLOY.md
   (after the title heading, before existing content).

6. **Report** — show the final section that was written and the line number it was inserted at.
