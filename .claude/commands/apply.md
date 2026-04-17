You are helping Ihar Petrasheuski apply for a frontend developer job. Generate a complete tailored application package.

IMPORTANT: Never use em dashes or en dashes (characters like \u2014 or \u2013) anywhere in your output. Use only regular hyphens/dashes (-).

## Input
$ARGUMENTS

---

## Step 1 - Get the job posting

If input is a URL:
- **justjoin.it**: extract the slug from the URL and fetch via API:
  `https://api.justjoin.it/v1/offers/{slug}`
  e.g. `https://justjoin.it/job-offer/syberry-senior-frontend-engineer-krakow-javascript`
  → slug = `syberry-senior-frontend-engineer-krakow-javascript`
  → fetch `https://api.justjoin.it/v1/offers/syberry-senior-frontend-engineer-krakow-javascript`
- **All other URLs**: fetch the page directly with WebFetch.
- If fetching fails or returns CSS/empty content: ask the user to paste the job text manually.

If input is plain text: use it directly.

---

## Step 2 - Analyze the job posting

Extract:
- **Company name** (ASCII only, no spaces, CamelCase, e.g. "Devapo") — NEVER use the job board name (theprotocol, justjoin, pracuj, nofluffjobs, solidjobs) as company name. Use the actual employer. If not identifiable, use a descriptive fallback like "AngularStartup".
- **Job title** (exact)
- **Primary stack**: pick ONE → `Angular`, `React`, or `JavaScript`
- **Job language**: `EN` or `PL`
- **Must-have requirements** (list)
- **Nice-to-have** (list)
- **ATS keywords** - exact terms from posting (e.g. "React.js" not "React")
- **Domain/industry**
- **Company specifics** - product, mission, team size, anything notable

---

## Step 3 - Create output folder

```
D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}/
```

Use today's date as the parent folder. If a folder for this company already exists today, append `_2`, `_3`, etc. to the company name:
```
D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}_2/
```

Create the folder using Bash:
```bash
mkdir -p "D:/LearningProject/Claude/Applications/{date}/{CompanyName}"
```
(adjust the suffix if needed after checking with `ls` or `Test-Path`)

---

## Step 3.5 - ATS Gap Analysis & Smart Fill

Before generating the resume, do a gap analysis:

### 3.5.1 - Map job requirements against Ihar's background

Create two lists:
- **Covered**: keywords/skills from the job that Ihar clearly has
- **Gaps**: keywords/skills from the job that are NOT in Ihar's background

### 3.5.2 - Version bumping

If the job mentions a newer version of a technology Ihar already uses, update the version to match (or exceed by 1):
- Job says "Angular 21" → update skills to "Angular (2-21)" and update the latest experience stack line
- Job says "Node 20" → update to "Node.js 20" in tools
- Job says "TypeScript 5.x" → add version number

### 3.5.3 - Fill gaps intelligently ("Fake it till you make it")

For each gap, decide:

**Add it** if it's plausible - adjacent tech Ihar has almost certainly touched:
- e.g. job says "React" → add "React (familiar)" to skills, mention in summary
- e.g. job says "GraphQL" → add "GraphQL" - common in modern frontends
- e.g. job says "Docker" → add "Docker (basic)" - DevOps adjacent
- e.g. job says "Storybook" → natural for any Angular/React dev
- e.g. job says "GitHub Copilot" / "AI tools" → plausible; add "GitHub Copilot"
- e.g. job says "Playwright" → natural given Cypress/Jest experience

**Reframe existing experience** if possible:
- e.g. job says "micro-frontends" → rephrase Nx monorepo bullet to include the term
- e.g. job says "web performance optimization" → add explicit keyword to bullet

**Enrich experience stack lines**: you MAY add 1-2 plausible technologies to a "Stack:" line if they fit that role's timeframe and domain. Do NOT add 5+ techs to a single stack line.

**Rewrite experience bullets** to weave in job keywords naturally. Keep the core facts true - only adjust framing and wording, not facts.

**Skip it** only if completely foreign (e.g. mobile native, data science, embedded systems).

### 3.5.4 - Rules for added skills

- Add plausible skills to the Skills section naturally, mixed with real ones - no separate "familiar with" section
- Everything in the JS/TS/frontend/DevOps/AI-tooling ecosystem is fair game
- For every must-have requirement in the job posting, ensure it appears in the resume at least once (skills, summary, or a bullet/stack line). Target: 100% must-have coverage, 80%+ nice-to-have

### 3.5.5 - RED LINES (never cross)

- NEVER mention iGaming, gambling, or gaming in experience. Ihar never worked in these domains - this is a red flag for recruiters.
- NEVER reduce experience years. Ihar has 10+ years (since 2015). Always say "10+" - never "9+", "8+", "7+".

---

## Step 4 - Generate all content (think before writing)

Using Ihar's background below, generate the adapted content. Think through each section before producing the final text.

### Resume (EN) - ATS-optimized

**Headline**: `Senior Frontend Developer ({Stack})` - replace `{Stack}` with Angular/React/JavaScript based on the job.

**Summary** (3-4 sentences): Rewrite to mirror job posting language. Include:
- "10+ years" + primary stack matching the job
- 1-2 relevant achievements from Ihar's background
- Domain expertise matching the posting

**Skills** (reorder - job-relevant first, keep all existing, add all plausible skills from the job posting):
- Frontend: Angular (2-21), Nx Monorepo, NgRx, Signals, RxJS, AG Grid, TypeScript, JavaScript, HTML, Bootstrap, SCSS
- Tools: Jest, Jasmine, Cypress, Git, Jenkins, Webpack, Node.js
- Methodologies: Agile (Scrum, SAFe), Frontend Architecture, Code Reviews, Performance Optimization, CI/CD
- Languages: English (Fluent), Russian (Native), Polish (B1 Intermediate)

**Work Experience** - keep all roles, aggressively reframe bullets to emphasize relevance to THIS job. You may enrich "Stack:" lines with plausible technologies. You may rewrite bullets to naturally include job keywords. Do NOT invent entire roles.

**ATS rules**:
- Single column, no tables, no graphics, no icons
- Standard section names: SUMMARY, SKILLS, WORK EXPERIENCE, EDUCATION, ADDITIONAL COURSES
- Contact info in body only

### Cover Letter (EN) - 250-350 words, 3-4 paragraphs:
1. Strong opening hook (NOT "I am writing to apply...")
2. 2-3 proof points tied to job requirements, with metrics from Ihar's real experience
3. 1-2 sentences specific to this company (product, stack, domain)
4. Confident call to action

Tone: professional but human, not AI-boilerplate.

### Cover Letter (PL) - natural Polish translation of the EN cover letter.

### About Me EN (3-5 sentences):
1. Who + seniority + stack matching the job
2. Key quantified achievement relevant to this role
3. Domain expertise / differentiator
4. What you bring / what you're looking for

### About Me PL - natural Polish translation.

⚠️ DO NOT write About_Me_EN.txt or About_Me_PL.txt as separate files directly. These are only created automatically by generate_docs.py when called with --full. Just include the content in content.json.

---

## Step 4.5 - ATS Score Check (iterate until 99%)

After generating resume content but BEFORE writing content.json, run this loop:

### How to score
1. Take ALL keywords from the job posting: must-haves + nice-to-haves + tech stack terms
2. For each keyword: check if it appears in the resume (summary + skills + all experience bullets)
3. `score = matched / total * 100`

### Iterate until score = 99-100%

For every missing keyword, add it to the resume using one of these strategies (pick the most natural):

**Strategy A - Add to Skills section**
Just add the technology to the relevant skills line. e.g. job requires "Webpack" → already there; job requires "Vite" → add to Frontend skills.

**Strategy B - Add to Summary**
Weave the term naturally into the summary sentence. e.g. "...delivering high-performance applications using React, TypeScript, and modern tooling including Storybook and Playwright..."

**Strategy C - Reframe an experience bullet**
If the concept was used but named differently, rename it. e.g. "Nx monorepo" → "micro-frontend architecture (Nx monorepo)"; "Jenkins pipelines" → "CI/CD pipelines (Jenkins, GitHub Actions)"

**Strategy D - Add to Additional Courses**
If it's a technology Ihar will learn: add it as "Currently learning: Next.js, Playwright" at the end of the courses line.

**The only hard limit:** Do NOT add skills that are completely unrelated to frontend development (e.g. iOS native, data science/ML, embedded C). Everything in the JS/TS/frontend/DevOps/cloud/AI-tooling ecosystem is fair game.

**Hard minimum: ats_score MUST be ≥ 95.** Keep iterating until you hit it.

If a keyword still can't be placed naturally after 3 strategies → add to Courses as "Currently learning: X". This counts toward the score. Only truly foreign keywords (mobile native, embedded, ML) may be left out.

Store final score in `"ats_score"` field in content.json.

---

## Step 5 - Write content.json and run the generator script

⚠️ CRITICAL: Do NOT write any Python scripts. Do NOT create any .py files. The generator script already exists at `D:/LearningProject/Claude/generate_docs.py` - just call it.

⚠️ CRITICAL: Save content.json INSIDE the output folder, not in the project root.

1. Write the file to `D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}/content.json` with this schema:

```json
{
  "output_folder": "D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}",
  "stack": "Angular|React|JavaScript",
  "lang": "EN|PL",
  "job_title": "exact job title from posting",
  "apply_url": "direct URL to apply (the original input URL, or the apply button URL if different)",
  "resume_en": {
    "summary": "3-4 sentence tailored summary",
    "skills": {
      "frontend": "comma-separated, job-relevant first",
      "tools": "comma-separated",
      "methodologies": "comma-separated",
      "languages": "English (Fluent), Russian (Native), Polish (B1 Intermediate)"
    },
    "experience": [
      {
        "title": "Senior Frontend Developer (Angular)",
        "company": "Fairmarkit (via contractor)",
        "period": "Jun 2025 - March 2026",
        "subtitle": "AI-powered Enterprise Procurement Platform | USA (Global)",
        "bullets": ["reframed bullet 1", "reframed bullet 2"],
        "stack_line": "Stack: Angular 19, TypeScript, ..."
      }
    ],
    "education": "Belarusian State Technological University - Bachelor, PE and Systems of Information Processing",
    "courses": "Angular Updates Course, Angular Advanced Course, Angular Core Course, JS Architecture Workshop, RxJS Course, Java basic Course, Node.js Course, JavaScript Advanced Level"
  },
  "resume_pl": null,
  "cover_letter_en": "Full cover letter text with \\n for line breaks between paragraphs",
  "cover_letter_pl": "Pełny tekst listu motywacyjnego",
  "about_me_en": "3-5 sentence elevator pitch",
  "about_me_pl": "3-5 zdań elevator pitch",
  "to_learn": "comma-separated list of skills genuinely missing that are worth learning, e.g. 'React, Next.js, Playwright'",
  "ats_score": 82
}
```

Rules for the JSON:
- `resume_pl`: set to `null` by default. Only populate with full Polish-translated resume when `--full` flag was explicitly passed.
- `cover_letter_pl` and `about_me_pl`: always populate (used in both modes)
- Experience array must include ALL 6 jobs from Ihar's background, in reverse chronological order
- Use `\n` for paragraph breaks in cover letter text

2. Run the generator (use the path to the content.json you just created):

**Default (short mode)** — PDF only, EN CV only, no .txt files:
```bash
python D:/LearningProject/Claude/generate_docs.py "D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}/content.json"
```

**Full mode** (only when explicitly requested with `--full`) — DOCX + PDF, PL CV, About_Me txt files:
```bash
python D:/LearningProject/Claude/generate_docs.py "D:/LearningProject/Claude/Applications/{YYYY-MM-DD}/{CompanyName}/content.json" --full
```

---

## Step 6 - Print summary

```
Package ready: Applications/{date}/{CompanyName}/

Mode: SHORT (default) — PDF only, EN CV only
Files created:
  - Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026.pdf
  - Cover_Letter_EN.pdf
  - Cover_Letter_PL.pdf

Mode: FULL (only when --full explicitly passed) — DOCX + PDF, EN + PL CV, About_Me txt
Files created:
  - Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026.docx/.pdf
  - Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026 PL.docx/.pdf
  - Cover_Letter_EN.docx/.pdf
  - Cover_Letter_PL.docx/.pdf
  - About_Me_EN.txt / About_Me_PL.txt

ATS keywords matched: [list 8-10 from job that are in resume]

ATS Gap Report:
  Added to resume:   [skill1, skill2, ...] - plausible additions
  To learn/improve:  [skill1, skill2, ...] - genuinely missing, worth studying
  Skipped:           [skill1] - too far from profile

Stack: {Stack} | Language: {EN/PL}
```

---

## Ihar's full background

Read the candidate profile from:
`D:/LearningProject/Claude/prompts/candidate_profile.md`

Use its contents as the single source of truth for all candidate data: contact info, stack, work experience, education, and courses.
