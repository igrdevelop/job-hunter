# About Me Agent — Implementation Plan

**Owner context:** Job-hunter bot for Ihar Petrasheuski (Senior Angular/AI, Wrocław).
Candidate profile: `prompts/candidate_profile.md`. Bot entry: `hunter.py`.
Read `CLAUDE.md` before touching anything.

**Goal:** Extract About Me generation into a dedicated reusable agent.
Call it automatically on `--full` and on demand via Telegram `/about_me` command.

**Scope:** New module, one new Telegram command, small changes to `generate_docs.py`.
No changes to `candidate_profile.md`, `system_prompt.md`, tracker schema, or scrapers.

---

## Current state (before this plan)

- About Me is generated as one field inside the big LLM call in `apply_agent.py`
- `generate_docs.py` writes `content["about_me_en"]` / `content["about_me_pl"]`
  directly to `About_Me_EN.txt` / `About_Me_PL.txt` — only in `--full` mode
- `prompts/examples/about_me_en.md` and `about_me_pl.md` exist but are NOT
  read anywhere in the pipeline — pure dead reference files
- Quality is poor: LLM spreads attention across 10 JSON fields, About Me gets weak output
- `about_me_en.md` has Angular 19 (stale), no Fairmarkit AI detail, no Cursor/Claude mention

---

## Step 0 — Update example files (do this FIRST)

These files will become few-shot examples in the agent prompt.
They must be accurate before the agent reads them.

### 0.1 — Update `prompts/examples/about_me_en.md`

**Find and replace throughout the file:**
- `Angular 19` → `Angular 21`

**In Version B (~130 words) and Version C (~350 words), update the Fairmarkit paragraph.**

Current (vague):
> Most recently, I worked on an AI-powered procurement platform where we developed
> complex workflow features, automation dashboards, and integrations involving
> AI-related functionality. The application was built with Angular 19 in an Nx monorepo...

Replace with (specific):
> Most recently, I worked on an AI-powered procurement platform at Fairmarkit, built
> with Angular 21 in an Nx monorepo. I delivered two AI-integrated features: one that
> consumed LLM-generated recommendations to help procurement teams select optimal
> suppliers from large historical + real-time datasets; another that consolidated
> fragmented workflows into a unified AG Grid view, with server-side LLM surfacing
> best-fit vendor options per client. I developed throughout using Cursor and Claude
> as agentic coding tools, and also contributed an automation dashboard and
> participated in frontend architecture decisions within a ~200-person engineering org.

**In Version A (~80 words)** — shorter update, just fix Angular version and add one AI sentence:
Replace:
> In my recent projects, I worked on banking applications for German cooperative banks
> and an AI-powered procurement platform built with Angular 19.

With:
> In my recent projects, I built LLM-integrated procurement features at Fairmarkit
> (Angular 21) and banking applications serving 300+ German cooperative banks.

### 0.2 — Update `prompts/examples/about_me_pl.md`

Current Polish version is very short (one paragraph) and generic — no metrics, no AI.

Replace the body with a proper updated version that:
- Mentions Fairmarkit AI features concretely (LLM-based decision support, workflow consolidation)
- Mentions 300+ German banks (Venture Labs)
- Uses Angular 21
- Is naturally written Polish (not a translation)
- No "pasjonat", no "udowodniony track record", no "idealnym kandydatem"

Example replacement for Version A:
> Jestem Senior Frontend Developerem z ponad 10-letnim doświadczeniem w Angular
> i nowoczesnej architekturze frontend. Ostatnio w Fairmarkit budowałem dwie funkcje
> z integracją LLM: narzędzie wspomagające decyzje zakupowe na podstawie dużych
> zbiorów danych oraz moduł konsolidujący workflow z rekomendacjami LLM po stronie
> serwera. Wcześniej w Venture Labs zbudowałem od podstaw aplikacje Angular 21
> używane przez ponad 300 niemieckich banków spółdzielczych. Projekty rozwijałem
> z użyciem narzędzi AI (Cursor, Claude), co skraca czas dostarczenia bez
> obniżania jakości kodu.

### 0.3 — Verify `about_me_legacy.md`

No changes needed — it's marked "DO NOT use as template" and serves as negative example.
Just confirm it's still accurate as a "what to avoid" guide.

---

## Step 1 — Create `hunter/about_me_agent.py`

New module. Single public function callable by both `generate_docs.py` and the Telegram handler.

### Function signature

```python
def generate_about_me(folder: Path, lang: str) -> str:
    """Generate About Me text for a job application folder.

    Args:
        folder: Path to Applications/{date}/{Company}/ — must contain job_posting.txt
        lang:   "en" or "pl"

    Returns:
        Generated About Me text (plain string, ready to write to file / send to Telegram).
        Returns "" on failure (logs error, does not raise).

    Side effect:
        Saves About_Me_EN.txt or About_Me_PL.txt inside folder.
    """
```

### Inputs the function reads

1. `folder / "job_posting.txt"` — full job posting text (required; return "" if missing)
2. `folder / "content.json"` — optional; if exists, extract `company_name`, `stack`,
   `job_title` to avoid re-asking LLM for already-parsed fields
3. `prompts/candidate_profile.md` — candidate facts (resolved relative to PROJECT_DIR)
4. `prompts/examples/about_me_en.md` — few-shot EN examples (all 3 versions)
5. `prompts/examples/about_me_pl.md` — few-shot PL example

### Prompt design

System prompt (keep it SHORT — this is not system_prompt.md):

```
You are writing a professional "About Me" / self-introduction for a senior developer's
job application. Return ONLY valid JSON: {"about_me": "<text>"}.
No markdown fences, no extra keys.
IMPORTANT: Never use em dashes or en dashes. Use only regular hyphens.
```

User message structure:
```
## Candidate Profile
{candidate_profile.md content}

---

## Target Job
Company: {company_name}
Title: {job_title}
Stack: {stack}

{job_posting.txt content, first 2000 chars}

---

## Examples of good About Me texts ({lang})
{about_me_en.md OR about_me_pl.md content}

---

## Task
Write About Me in {ENGLISH/POLISH} tailored to THIS job.
Length: 4-6 sentences (~100-150 words).
Lead with: seniority + stack match to this role.
Include: at least 1 quantified metric from experience.
Highlight: what is MOST relevant to this specific role (match to must-haves).

{PL only}: Write natively in Polish — NOT a translation. Use Polish idioms.

BANNED phrases:
- EN: proven track record, passionate about, excited to, thrilled to, leverage,
  aligns with my background, seamlessly, comfortable with, perfect fit, ideal match
- PL: udowodniony track record, pasjonat, idealnym kandydatem, doskonale wpisuje się
```

### Output

- Call `llm_client.call_llm()` with the above prompts
- Parse `result["about_me"]`
- Save to `folder / f"About_Me_{lang.upper()}.txt"` (UTF-8)
- Print `[about_me_agent] Saved About_Me_{LANG}.txt`
- Return the text string

### Error handling

- `job_posting.txt` missing → log warning, return ""
- LLM call fails → log error, return ""
- JSON parse fails → try `result` as raw string fallback, else return ""

### Config

Import from `hunter.config`:
- `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `PROJECT_DIR`

---

## Step 2 — Integrate into `generate_docs.py` (`--full` mode)

**File:** `generate_docs.py`, around lines 341-351

**Current code:**
```python
if full_mode:
    if content.get("about_me_en"):
        p = Path(output_folder) / "About_Me_EN.txt"
        p.write_text(content["about_me_en"], encoding="utf-8")

    if content.get("about_me_pl"):
        p = Path(output_folder) / "About_Me_PL.txt"
        p.write_text(content["about_me_pl"], encoding="utf-8")
```

**Replace with:**
```python
if full_mode:
    from hunter.about_me_agent import generate_about_me
    generate_about_me(Path(output_folder), lang="en")
    generate_about_me(Path(output_folder), lang="pl")
```

Note: `generate_about_me` handles saving internally. The old content["about_me_*"]
fields from the big LLM call are now ignored for file output (still in content.json
for reference but not written to disk directly).

---

## Step 3 — Tracker lookup helper

**File:** `hunter/tracker.py`

Add one new function (append at end of file):

```python
def get_folder_by_url(url: str) -> str | None:
    """Return the Folder value for a given job URL, or None if not found.

    Normalizes the URL before comparing (strip trailing slash, lowercase scheme).
    Returns the raw string from the Folder column (e.g. 'Applications/2026-05-11/PeopleMore_3').
    """
```

Implementation:
- Open tracker.xlsx with `openpyxl` (read_only=True)
- Normalize input url with `normalize_url()` (already exists in tracker.py)
- Iterate rows, compare normalized URL (column 6, index 5) with normalized input
- Return column 7 (index 6) value when matched
- Return None if not found

---

## Step 4 — Telegram command `/about_me`

**File:** `hunter/telegram_bot.py`

### Command format

```
/about_me pl https://justjoin.it/job-offer/...
/about_me en https://justjoin.it/job-offer/...
```

### Handler

```python
async def cmd_about_me(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Generate (or regenerate) About Me for a job URL in the tracker.

    Usage: /about_me <lang> <url>
    lang: en | pl
    """
```

Algorithm:
1. Parse `context.args` → expect exactly 2 args: lang + url
   - If wrong format → reply with usage hint
2. Validate lang is "en" or "pl"
3. Normalize url
4. Call `get_folder_by_url(url)` from tracker
   - Not found → reply "URL not found in tracker. Run /force to process it first."
5. Resolve full folder path: `PROJECT_DIR / folder_str`
   - `job_posting.txt` missing → reply "No job_posting.txt in folder — cannot generate."
6. Send "⏳ Generating About Me ({lang.upper()})..." to Telegram
7. Call `await asyncio.to_thread(generate_about_me, folder_path, lang)`
8. If result is empty → reply "❌ Generation failed — check logs."
9. Send result text to Telegram (plain text, no markdown to avoid formatting issues)
10. Confirm: "✅ Saved to {folder}/About_Me_{LANG}.txt"

### Register the command

In `build_application()`:
```python
app.add_handler(CommandHandler("about_me", cmd_about_me))
```

In `_set_bot_commands()`:
```python
BotCommand("about_me", "Generate About Me for a job URL (lang + url)"),
```

---

## Step 5 — Verification

1. `python -m compileall .` — must pass, no errors
2. Restart bot (`python hunter.py`)
3. Manual test: `/about_me pl https://justjoin.it/job-offer/people-more-p-s-a--ai-expert-agentic-ai-llm-angular-jsp--krakow-ai`
   - Bot replies with Polish About Me text
   - File `Applications/2026-05-11/PeopleMore_3/About_Me_PL.txt` created / updated
4. Manual test: `/about_me en <same url>` — EN version
5. Test `--full` mode on a fresh URL:
   `python apply_agent.py <url> --full --force`
   - Verify `About_Me_EN.txt` and `About_Me_PL.txt` created in output folder
   - Verify content is tailored (not generic)
6. Error case: `/about_me pl https://example.com/nonexistent` → "URL not found in tracker"

---

## Files changed

| File | Change |
|------|--------|
| `prompts/examples/about_me_en.md` | Step 0.1: Angular 21, Fairmarkit AI details |
| `prompts/examples/about_me_pl.md` | Step 0.2: new Polish version with metrics |
| `hunter/about_me_agent.py` | Step 1: NEW — core generator |
| `generate_docs.py` | Step 2: call agent instead of writing from content.json |
| `hunter/tracker.py` | Step 3: add `get_folder_by_url()` |
| `hunter/telegram_bot.py` | Step 4: new handler + registration |

## Out of scope — do NOT do

- Changing `candidate_profile.md` (already updated separately)
- Changing `system_prompt.md` or main LLM call in `apply_agent.py`
- Generating About Me DOCX or PDF (only .txt)
- Supporting About Me without an existing tracker entry
- Changing tracker schema

---

## Progress log

| Date | Agent | Status |
|------|-------|--------|
| 2026-05-13 | sonnet-4-6 | Plan created |
| 2026-05-13 | sonnet-4-6 | Step 0 DONE: examples updated (see below) |
| 2026-05-13 | sonnet-4-6 | Steps 1-4 DONE: about_me_agent.py, generate_docs.py, tracker.py, telegram_bot.py |
| 2026-05-13 | sonnet-4-6 | Step 5 DONE: all manual tests passed (PL, EN, invalid URL error) |

### Step 0 results

**`prompts/examples/about_me_en.md`:**
- Version A: Fairmarkit AI mention added, Angular 21, 300+ banks metric
- Version B: full Fairmarkit AI paragraph rewritten (LLM features, Cursor+Claude, ~200-person org), Venture Labs updated (300+ banks, Angular 14→21, SonarQube/Cypress)
- Version C: same Fairmarkit paragraph + Venture Labs update applied

**`prompts/examples/about_me_pl.md`:**
- Old Version A (generic, no metrics, no AI) replaced with:
  - New Version A (~80 words): Fairmarkit LLM features + 300+ banks + Cursor/Claude
  - New Version B (~150 words): detailed version with both projects, migration scope, remote/Wroclaw

**`prompts/examples/about_me_legacy.md`:** no changes needed (negative example, still valid)

**Status: COMPLETE** — all steps done and verified 2026-05-13
