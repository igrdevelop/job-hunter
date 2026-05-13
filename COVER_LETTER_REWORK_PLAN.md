# Cover Letter Rework Plan

Branch: `feature/cover-letter-rework` (from `develop`)
Created: 2026-05-13

---

## Problem

Current cover letters sound like AI-generated boilerplate despite a detailed prompt in `system_prompt.md`.

**Root causes:**
1. Prompt explicitly allows AI phrases ("I am writing to express my interest", "Thank you for considering my application", "As you may see from my attached resume")
2. No real examples for tone reference — agent has no anchor for what "good" looks like
3. `build_cover_letter` in `generate_docs.py` produces bare text — no letterhead, no recipient, no signature with contacts
4. CL instructions duplicated in two places (`apply.md` and `system_prompt.md`) with conflicts
5. No way to regenerate just the CL without re-running full `/apply`

**Evidence:**
- ClickHouse CL opens with: "Building developer tools that cut through the noise when production breaks at 2 AM - that is the challenge I am most excited to work on."
- Airbnb CL ends with: "I would welcome the chance to discuss..."
- Sample from `tools/output/sample_cover_classic_en.txt` opens with: "I am writing to express my interest..."
- All lack letterhead, date, recipient block, contact info in signature

---

## Target state

- Opener: direct, one sentence — "I am applying for [role]." (EN) / "Szanowni Panstwo, aplikuje na stanowisko [role]." (PL)
- Body: 2-3 short paragraphs, each = 1 project + 1 metric
- Length: 150-220 words EN / 120-180 words PL
- DOCX: proper business letter with letterhead, date, recipient, signature block
- Tone: matches the 15 EN/PL examples in `prompts/examples/`
- Single source of truth for CL rules: `system_prompt.md`
- Separate `/cover-letter` command for iteration

---

## Current state audit (develop branch)

| Component | Status | Problem |
|-----------|--------|---------|
| `prompts/system_prompt.md` CL section | ~100 lines, two-layer model, story bank, banned phrases | Allows AI-tone openers and closings |
| `generate_docs.py` `build_cover_letter` | 6 lines, bare text split by `\n` | No letterhead, recipient, date, signature |
| `prompts/examples/` | Just created (not yet committed) | Prompt doesn't reference them |
| `tools/regen_covers_v2_last3.py` | Works, calls LLM with `system_prompt.md` | Inherits all prompt problems |
| `.claude/commands/apply.md` | Has own CL instructions (lines 134-142) | Duplicates/conflicts with `system_prompt.md` |
| `.claude/commands/cover-letter.md` | Does not exist | Cannot iterate CL without full `/apply` |

---

## Phase 1 — Examples as tone anchor

**Status: DONE** (files created, not committed)

Files created in `prompts/examples/`:
- `cl_examples_en.md` — 15 EN senior cover letter examples + market principles
- `cl_examples_pl.md` — 15 PL senior cover letter examples + market principles
- `about_me_en.md` — 3 versions (Short ~80w / Medium ~130w / Full ~350w)
- `about_me_pl.md` — PL version
- `about_me_legacy.md` — old version from ~2 years ago, annotated with what's wrong (reference only)

---

## Phase 2 — Clean up CL prompt in `system_prompt.md`

### 2.1 — Replace Layer A opener permission

**Current** (lines ~108-110 in CL section):
```
Standard phrases are OK: I am writing to express my interest...,
I would like to apply for..., My name is ... and I am writing in response to...,
I was interested to read your advertisement for...
```

**Replace with:**
```
Opening: one direct sentence. State the role and (optionally) where you found it.
Allowed: "I am applying for the [exact role title] position."
PL: "Szanowni Panstwo, aplikuje na stanowisko [exact role title]."
No hooks. No embellishment. No "I am excited/interested/passionate."
```

### 2.2 — Replace closing permission

**Current:**
```
Fully allowed: Thank you for considering my application,
I look forward to meeting you, I look forward to discussing my qualifications / this role
```

**Replace with:**
```
Closing: one concrete sentence — availability, willingness to discuss a specific topic,
or timezone for a call.
Forbidden closings:
- "Thank you for considering my application"
- "I would welcome the opportunity/chance to discuss..."
- "I look forward to hearing from you"
Allowed: "I am available for a call to discuss [specific topic from the posting]."
```

### 2.3 — Reduce word count

**Current:** ~180-280 words
**Replace with:** 150-220 words EN / 120-180 words PL

### 2.4 — Add reference to examples

Add before the CL section:
```
Before writing any cover letter, read the examples in prompts/examples/cl_examples_en.md
(or cl_examples_pl.md for PL letters). Match their brevity and directness.
Each body paragraph = one specific project + one metric. No filler.
```

### 2.5 — Remove "attached resume" references

**Delete from allowed phrases:**
- "As you may see from my attached resume..."
- "As detailed in my attached resume..."

These are 2005-era textbook filler.

### 2.6 — Add to banned phrases list

Add these to the existing banned list:
```
- "I am writing to express my interest..."
- "Thank you for considering my application"
- "I would welcome the opportunity..."
- "I would welcome the chance..."
- "As you may see from my attached resume..."
- "As detailed in my attached resume..."
```

---

## Phase 3 — Rewrite `build_cover_letter` in `generate_docs.py`

### 3.1 — New function signature

```python
# Current (line ~204):
def build_cover_letter(doc, text):

# New:
def build_cover_letter(doc, text, company_name, job_title, date_str):
```

### 3.2 — DOCX structure the function will produce

```
Ihar Petrasheuski (Igor Pietraszewski)                    [16pt bold, centered]
igrflex@gmail.com | +48 571 525 110 | linkedin.com/in/ijerweb   [10pt, centered]
Wroclaw, Poland                                            [10pt, centered]

[horizontal line]

May 13, 2026                                               [11pt, left-aligned]

[Company Name] - Hiring Team                               [11pt, left-aligned]
Re: [Job Title]                                            [11pt, italic]

                                                           [blank paragraph]

[Body paragraphs from text, split by \n]                   [11pt, after=8pt]

                                                           [blank paragraph]

Best regards,                                              [11pt]
Ihar Petrasheuski                                          [11pt, bold]
igrflex@gmail.com | +48 571 525 110                        [10pt]
```

### 3.3 — Update call sites

Lines ~300-312 in `generate_docs.py` — pass extra params:
```python
# Current:
build_cover_letter(doc, content["cover_letter_en"])

# New:
build_cover_letter(
    doc,
    content["cover_letter_en"],
    company_name=content.get("company_name", ""),
    job_title=content.get("job_title", ""),
    date_str=date.today().strftime("%B %d, %Y"),
)
```

### 3.4 — Update content.json prompt instructions

In `system_prompt.md` Output JSON section, add note:
```
Do NOT include signature block (Best regards / name) in cover_letter_en or cover_letter_pl
text — the DOCX template adds it automatically. End the text after the last body paragraph.
```

This is partially there already ("the DOCX template handles that") but needs to be explicit in the JSON schema section too.

---

## Phase 4 — New `/cover-letter` command

### Create `.claude/commands/cover-letter.md`

**Input:** `$ARGUMENTS` — either a path to `content.json` or a job posting URL/text

**Mode 1 — Regenerate (path to content.json):**
1. Read the existing `content.json`
2. Read the job posting from `job_posting.txt` in the same folder (or from `apply_url`)
3. Generate new `cover_letter_en` and `cover_letter_pl` using rules from `system_prompt.md`
4. Read `prompts/examples/cl_examples_en.md` and `cl_examples_pl.md` for tone
5. Update `cover_letter_en` and `cover_letter_pl` in `content.json`
6. Run `python generate_docs.py <content.json>` to rebuild DOCX/PDF

**Mode 2 — Standalone (URL or text):**
1. Fetch/parse the job posting
2. Extract company name, job title, stack
3. Generate CL only (no resume)
4. Print to console

**Quality gate (both modes):**
```
Before outputting, verify:
[ ] No forbidden phrases (check against banned list)
[ ] Opening is one direct sentence with role title
[ ] At least 2 metrics in body (numbers, %, scale)
[ ] Company name appears in body
[ ] Word count: 150-220 EN / 120-180 PL
[ ] No signature block in text (DOCX adds it)
```

---

## Phase 5 — Sync `/apply` with `system_prompt.md`

### In `.claude/commands/apply.md`

**Delete** lines 134-142 (current CL instructions):
```
### Cover Letter (EN) - 250-350 words, 3-4 paragraphs:
1. Strong opening hook (NOT "I am writing to apply...")
...
### Cover Letter (PL) - natural Polish translation of the EN cover letter.
```

**Replace with:**
```
### Cover Letter

Follow the Cover Letter rules in prompts/system_prompt.md exactly.
Read prompts/examples/cl_examples_en.md (or _pl.md) for tone reference before writing.
Match the examples' brevity: 150-220 words EN, 120-180 words PL.
Each body paragraph = one project + one metric. No filler sentences.
```

Single source of truth: `system_prompt.md`.

---

## Phase 6 — Update `tools/regen_covers_v2_last3.py`

This script already calls LLM with `system_prompt.md`. After Phase 2 changes, it automatically picks up new rules. Additional changes:

1. Add examples to the system prompt it sends:
   ```python
   examples_en = (PROJECT_DIR / "prompts" / "examples" / "cl_examples_en.md").read_text()
   system = _load_system_prompt() + "\n\n---\n\n" + examples_en
   ```

2. Update `build_cover_letter` calls to use new signature (after Phase 3)

3. Read `company_name` and `job_title` from `content.json` when regenerating

---

## Implementation order

| # | What | File(s) | Dependencies | Testable? |
|---|------|---------|--------------|-----------|
| 1 | Letterhead in DOCX | `generate_docs.py` | None | Yes — run on any existing content.json |
| 2 | Clean CL prompt | `system_prompt.md` | None | Yes — run `tools/generate_sample_classic_cover.py` |
| 3 | Add examples reference | `system_prompt.md` | Phase 1 (done) | Yes — same test |
| 4 | Sync apply.md | `.claude/commands/apply.md` | After 2-3 | Manual — run `/apply` |
| 5 | New `/cover-letter` cmd | `.claude/commands/cover-letter.md` | After 2-3 | Manual — run `/cover-letter` |
| 6 | Update regen script | `tools/regen_covers_v2_last3.py` | After 1-3 | Yes — `python tools/regen_covers_v2_last3.py --count 1` |

---

## Acceptance criteria

- [ ] CL opener is direct: "I am applying for [role]." — no hooks, no emotions
- [ ] CL body has at least 2 metrics per letter
- [ ] CL length: 150-220 words EN / 120-180 words PL
- [ ] No forbidden phrases in output (tested against banned list)
- [ ] DOCX has letterhead (name, contacts, city, date)
- [ ] DOCX has recipient block (company name, "Re: job title")
- [ ] DOCX has signature with contacts (email, phone)
- [ ] `/cover-letter` command works standalone
- [ ] `/apply` defers to `system_prompt.md` for CL rules (no duplication)
- [ ] `regen_covers_v2_last3.py` produces letters matching new format
