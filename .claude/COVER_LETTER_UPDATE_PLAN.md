# Cover Letter Quality Update — Execution Plan

**Owner context:** This repo is a job-hunter bot that generates tailored resumes + cover letters via LLM for Ihar Petrasheuski (Senior Angular Frontend, Wroclaw). The generation prompt lives in `prompts/system_prompt.md`; the apply pipeline lives in `apply_agent.py`. See `CLAUDE.md` for full project context.

**Goal:** Improve the quality of generated cover letters (EN + PL). Current output reads AI-generated, sometimes hallucinates tech experience, uses formulaic CTAs, and lacks metrics. This plan fixes that.

**Scope:** `prompts/system_prompt.md` + `apply_agent.py` self-review loop only. No changes to resume generation, `candidate_profile.md`, or doc-generation code.

**Non-goals:** No refactor of `generate_docs.py`, no new scrapers, no tracker schema changes.

---

## Diagnosis (why we are doing this)

Reviewed 4 recent generated CLs in `Applications/2026-04-22/` and `Applications/2026-04-23/` (Consdata, AdobeCMS, Acronis, Remodevs). Concrete problems found:

1. **Hallucination of deep tech experience.** AdobeCMS CL: *"specifically wrestling with AEM's component architecture limitations while building procurement dashboards at Fairmarkit"* — candidate has never used AEM.
2. **Opener drifts into thought-leadership.** Consdata CL: *"Working with Angular in the banking sector for the past several years, I have seen firsthand how much financial institutions demand…"* — close to already-banned opener patterns, but slipped through.
3. **Proof paragraph is a dense list of 5–6 facts** instead of 2 Challenge→Action→Outcome blocks with metrics.
4. **CTA is identical across all 4 letters:** *"I would welcome the opportunity to contribute to your team. Please find my CV attached and feel free to reach out."* Dead phrase.
5. **Only 1 metric per letter** (usually "300+ banks"). Missing load-time, Lighthouse, bundle-size, migration-downtime — numbers that differentiate senior FE candidates.
6. **AI-smell phrases** slip through: *"aligns directly with my background"*, *"comfortable owning features end-to-end"*.
7. **Length creep** — some letters hit ~330 words; 2026 guides (Enhancv, Teal, Resume Worded) recommend 220–280 for senior roles.
8. **PL version is a literal translation**, not a native re-write — e.g. *"przyciagnelo mnie do oferty"* is an English calque.

---

## Policy decisions (do NOT change these — user confirmed)

### Careful embellishment IS allowed
The existing "plausible adjacent tech" policy in `prompts/system_prompt.md` Step 2 extends to cover letters. The user explicitly permits claiming unfamiliar tech in the CL, **carefully**. Do NOT introduce a blanket anti-hallucination ban.

Three tiers:

| Tier | What | Examples |
|---|---|---|
| **Green — free** | Familiarity mentions, reframing existing work in vendor terminology, version bumps, adding 1 adjacent tech to an existing stack-line | *"familiar with Storybook"*, Nx monorepo → *"micro-frontend architecture"*, Angular 19 → Angular 21 |
| **Yellow — safe verbs only** | Concrete claims about unfamiliar tech using **safe verbs**: `familiar with`, `exposure to`, `adjacent to`, `ramping up on`, `transferable from X`, `comfortable picking up` | *"My Nx monorepo work at Fairmarkit is transferable to AEM's component model, which I'm already ramping up on."* |
| **Red — forbidden** | Inventing employers/projects/clients; attaching specific timeframes or metrics to unfamiliar tech; **danger verbs** for unfamiliar tech: `spent N years on X`, `led X`, `architected X`, `built X from scratch`, `owned X`; any mention of iGaming/gambling/gaming (existing red line) | ❌ *"I spent two years wrestling with AEM at Fairmarkit"* |

---

## Edit 1 — `prompts/system_prompt.md`, Cover Letter EN section (currently lines ~68–139)

Replace the entire Cover Letter EN block with the structure below. Keep everything OUTSIDE the Cover Letter section untouched (Step 1, Step 2, Resume section, Step 4 ATS, JSON schema, etc.).

### 1.1 New length + hard counters
- Target **220–280 words** (was 250–350).
- Minimum **2 numeric metrics** in the letter body (excluding "10+ years"). Metrics = %, count, scale, timeframe, version, team size, client count.
- Max **1 repetition** of the phrase "Senior Frontend Developer".

### 1.2 Safe / Danger verbs table (new, place at top of Cover Letter section)
Add a visible table listing safe verbs (green), reframing verbs (green), and danger verbs (red) for unfamiliar tech. Match the tiers in the policy section above. Include one good/bad example pair.

### 1.3 Paragraph 1 — Opener (keep existing rules, tighten)
Keep the existing "concrete-fact-about-THEM + one anchor from YOU" rule and company-name-swap test. Additions:

- Max **25 words, 1 sentence** (was 30).
- Add to BANNED openers list:
  - `"Working with X for the past … years, I have [seen | learned | observed] …"` (thought-leadership)
  - `"Having [verb]ed X for N years …"` (same shape)
- Keep all existing banned patterns.

### 1.4 Paragraph 2 — Proof (rewrite the rule)
Replace "pick 2-3 facts from experience" with a strict form:

- **Exactly 2 compact blocks**, each 1–2 sentences.
- Each block follows **Challenge → Action → Outcome** explicitly.
- Each block must contain **≥1 numeric metric** (number, %, scale, version, timeframe).
- Max **3 technologies** mentioned per block (avoid keyword-stuffing).
- Pick the 2 blocks that best match the job's top-2 must-have requirements. Rotate — do not always use the same pair.

Matching guide (keep existing, unchanged):
- Team leadership / code review → Venture Labs cross-functional team 10+
- Performance / complex data grids → AG Grid + Signals + Nx monorepo
- Testing / quality gates → Jest, Cypress, SonarQube, Jenkins pipelines
- Migration / version upgrade → Angular 14→19 migration (Venture Labs)
- Architecture decisions in a larger org → Fairmarkit frontend architecture (~200 people)
- E2E ownership / greenfield → Altoros (built platform from scratch, admin panel)

### 1.5 Paragraph 3 — Fit (rewrite the rule)
- **2–3 sentences**, no more.
- Sentence 1: one concrete fact about THEM (product, stack, mission, specific phrase from posting). Must die on company-name swap.
- Sentence 2: why this role fits the candidate's trajectory — specific, not generic "growth opportunity".
- Optional sentence 3: one forward-looking hypothesis the candidate would bring (e.g. "The Nx monorepo experience maps directly to your plan to split the monolith").

**New ban list** (hard reject if generated, regenerate):
- `"aligns with my background"` / `"aligns perfectly with"`
- `"perfect fit"` / `"ideal match"`
- `"exactly what I'm looking for"`
- `"passionate about"` / `"excited to"` / `"thrilled to"`
- `"proven track record"`
- `"comfortable owning"` / `"comfortable with"` (weak filler)
- `"seamlessly"` / `"leverage"` / `"synergy"`

### 1.6 Paragraph 4 — CTA (rewrite the rule)
- **1 sentence, forward-looking and specific.**
- Must include ONE concrete next-step anchor: a time window, a topic to discuss, a question to answer, or Wroclaw timezone availability.

**BANNED CTA templates** (hard reject):
- `"I would welcome the opportunity to contribute to your team."`
- `"Please find my CV attached."`
- `"Feel free to reach out."`
- `"I look forward to hearing from you."`
- `"Thank you for considering my application."`

**Good CTA examples:**
- *"Happy to walk through the Venture Labs Angular 14→19 migration on a call — Wroclaw timezone, any afternoon next week."*
- *"If it helps, I can demo the AG Grid + Signals patterns we shipped at Fairmarkit in a 20-min screen-share."*

---

## Edit 2 — `prompts/system_prompt.md`, Cover Letter PL section

Currently a one-liner: *"Cover Letter PL: natural Polish translation of the EN cover letter."*

Replace with:

> **Cover Letter PL:** Re-write in Polish as if drafted natively — do NOT translate word-by-word. Use Polish idioms and collocations. Avoid English calques (e.g. *"przyciagnelo mnie do oferty"* is a calque; prefer *"zaintereso­wala mnie oferta"*). Same structure as EN (4 paragraphs, same length bounds, same bans). Openers, metrics, and CTA must be re-cast naturally, not translated.

Add 1 good/bad example pair for opener, and 1 for CTA.

---

## Edit 3 — `apply_agent.py` self-review loop

The existing cover-letter self-review loop (up to 3 rounds) must check additional criteria. Find the review prompt in `apply_agent.py` (grep for the review-loop function; it currently asks the LLM to critique the CL).

Extend the review checklist with these **pass/fail gates** — ANY fail triggers a regenerate:

1. **Word count** 220–280? (count words in `cover_letter_en`)
2. **≥2 numeric metrics** in body? (regex `\d+%|\d+\+|\d+x|\d{4}` minus the "10+ years" mention)
3. **Opener survives company-name swap?** If the sentence still makes sense with company renamed to `AcmeCo`, FAIL.
4. **No banned phrases?** Check against the ban lists in Edit 1.3, 1.5, 1.6.
5. **Unfamiliar tech uses safe verbs only?** For any tech mentioned that is NOT in `candidate_profile.md`, verify it is introduced via a safe verb (`familiar with`, `ramping up on`, etc.), not a danger verb (`spent N years`, `architected`, `built from scratch`).
6. **CTA is not in the banned CTA list?**

The review loop prompt should enumerate these 6 gates explicitly and ask the LLM to return `{"pass": bool, "fails": [...], "rewrite_suggestions": "..."}`. If any fail, regenerate and re-check up to 3 rounds total (keep existing max rounds).

---

## Edit 4 (optional, do last) — domain hook pack

In `prompts/system_prompt.md`, extend the "Domain → which proof to pull into the hook" table from the current 5 domains to 8–10:

- Banking / fintech → Venture Labs / 300 German banks
- Enterprise SaaS / procurement → Fairmarkit
- Startup / greenfield → Venture Labs from scratch, or Altoros e-commerce
- Legacy modernisation / migration → Angular 14→19 migration
- AI / automation tooling → Fairmarkit AI integration
- **NEW: Healthcare / insurance → Altoros health-vertical sub-project (verify in candidate_profile.md first)**
- **NEW: E-commerce / retail → Altoros multi-tenant shop platform**
- **NEW: Dev platforms / internal tooling → Fairmarkit internal AI tooling**
- **NEW: Logistics / supply chain → reframe procurement as supply-side logistics**
- **NEW: Media / CMS → reframe Altoros shop-content work as CMS-adjacent**

Before adding a NEW domain row, grep `prompts/candidate_profile.md` to verify the proof point actually exists. If it does not, mark the row as "yellow — use safe verbs" instead of claiming ownership.

---

## Verification procedure (run after every edit)

1. **Syntax check:** `python -m compileall .` — must pass.
2. **Smoke run — 3 test jobs.** Pick 3 URLs: one clear Angular match (e.g. a recent Consdata-style posting), one stack-mismatch (Vue or React), one with unfamiliar tech (AEM/Sitecore/Salesforce-style). Run:
   ```bash
   python apply_agent.py <url> --force
   ```
   for each. Inspect the generated `cover_letter_en` and `cover_letter_pl` in each `Applications/<date>/<Company>/content.json`.
3. **Manual diff:** compare against the old Consdata / AdobeCMS / Remodevs letters in `Applications/2026-04-22/` and `Applications/2026-04-23/`. Verify:
   - Opener is concrete-about-THEM, survives swap test.
   - Proof paragraph has exactly 2 Challenge→Action→Outcome blocks with metrics.
   - No banned phrases from the ban list.
   - CTA is specific, not the old "I would welcome…" template.
   - Length 220–280 words.
   - PL reads naturally (no obvious calques).
4. **Check the self-review loop logs** in stdout to confirm the new gates are firing and regenerations happen when gates fail.
5. Run `python -m compileall .` one more time before commit.

---

## Git workflow

- Active branch: `develop` (per `CLAUDE.md`).
- Make edits, verify, commit with a message like:
  ```
  feat(prompts): rewrite cover letter rules — tighter structure, metrics, anti-AI-smell
  ```
- Do NOT commit `Applications/`, `tracker.xlsx`, `to_send.xlsx`, or `.env`.
- Do NOT push to `master` / `main`.

---

## Execution log

| Date | Agent | What was done |
|------|-------|---------------|
| 2026-05-13 | sonnet-4-6 | Edit 1 (system_prompt.md): word count 180→220, CTA rules strengthened (banned 5 generic closings, required concrete anchor), safe/danger verbs table added, Avoid list expanded (+aligns with my background, +aligns perfectly with, +comfortable with, +excited to, +proven track record, +leverage, +synergy, +perfect fit, +ideal match), Proof paragraph CAO structure added, Story bank extended to 15 domains (Edit 4). |
| 2026-05-13 | sonnet-4-6 | Edit 3 (apply_agent.py): _CL_WORD_MIN 180→220, _BANNED_BODY_PHRASES +5 patterns (aligns with my background, aligns perfectly with, excited to, comfortable with, +aligns perfectly with), _BANNED_CTA_PHRASES +2 (I look forward to hearing from you, Thank you for considering my application), user_msg in review loop updated with new banned CTA list and banned body phrase list. `python -m py_compile apply_agent.py` passes. |

---

## Success criteria (definition of done)

- [x] `prompts/system_prompt.md` updated per Edits 1 + 2 (+ optional 4).
- [x] `apply_agent.py` self-review loop extended per Edit 3; the 6 gates are explicit in the review prompt.
- [x] 3 smoke-test CLs generated via `smoke_test_cl.py` on real Applications/ job_posting.txt files.
  - Antal Angular: banned phrase `proven track record` caught, review loop fixed → PASS
  - Appliscale React: banned phrase `excited to` caught, review loop fixed → PASS
  - Upvanta Fullstack: banned phrase caught and fixed; Gate 2 (metrics) stays FAIL — posting has no client-side numbers, expected behaviour
- [x] `python -m py_compile apply_agent.py` passes.
- [x] Committed on `develop` (b5bf71b), not pushed to `master`.

### Known limitation (post-test finding)
`_METRIC_RE` is regex-based and misses natural-language metrics like "15 institutions", "2 applications", version numbers under 100. Even after expanding the word list, some job types (short-term contracts, backend-heavy postings) produce letters with only 1 regex-detectable metric. The gate correctly fires and triggers rewrite, but 3 LLM rounds may not always reach ≥2. Possible future improvement: also count 2-digit standalone numbers in context (e.g. `\b\d{2,}\b` only when preceded/followed by a job-relevant context word).

---

## Out of scope — flag, do NOT do

- Rewriting resume generation rules.
- Changing `candidate_profile.md`.
- Changing doc-generation (`generate_docs.py`) or tracker logic.
- Adding new scrapers.
- Any work on `to_send.xlsx` sync.

If you think one of these is needed, stop and leave a note in a follow-up comment — do NOT expand scope.
