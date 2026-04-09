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
- **Company name** (ASCII only, no spaces, e.g. "Devapo")
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
D:/LearningProject/Claude/Applications/{CompanyName}_{YYYY-MM-DD}/
```

Use today's date. If that folder already exists (company applied twice today), append `_2`, `_3`, etc.:
```
D:/LearningProject/Claude/Applications/{CompanyName}_{YYYY-MM-DD}_2/
```

Create the folder using Bash: `mkdir -p "D:/LearningProject/Claude/Applications/{CompanyName}_{date}"`
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

**The only hard limit:** Do NOT add skills that are completely unrelated to frontend development (e.g. iOS native, data science, embedded C). Everything in the JS/TS/frontend/DevOps/AI-tooling ecosystem is fair game.

Keep iterating until every keyword from the job is present in the resume. Target: 100% must-have coverage, 80%+ nice-to-have.

If after 3 iterations a keyword truly cannot be placed naturally → mark as "unavoidable miss", do not force it.

Store final score in `"ats_score"` field in content.json.

---

## Step 5 - Write content.json and run the generator script

⚠️ CRITICAL: Do NOT write any Python scripts. Do NOT create any .py files. The generator script already exists at `D:/LearningProject/Claude/generate_docs.py` - just call it.

⚠️ CRITICAL: Save content.json INSIDE the output folder, not in the project root.

1. Write the file to `D:/LearningProject/Claude/Applications/{CompanyName}_{YYYY-MM-DD}/content.json` with this schema:

```json
{
  "output_folder": "D:/LearningProject/Claude/Applications/{CompanyName}_{YYYY-MM-DD}",
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
- `resume_pl`: set to `null` by default (short mode skips PL CV). Only populate with full Polish-translated resume data when `--full` flag is used.
- `cover_letter_pl` and `about_me_pl`: always populate (cover letter PL is still generated in short mode; about_me_pl is kept in JSON for reference even though the .txt file is only created in full mode)
- Experience array must include ALL 6 jobs from Ihar's background, in reverse chronological order
- Use `\n` for paragraph breaks in cover letter text

2. Run the generator (use the path to the content.json you just created):
```bash
python D:/LearningProject/Claude/generate_docs.py "D:/LearningProject/Claude/Applications/{CompanyName}_{YYYY-MM-DD}/content.json"
```

---

## Step 6 - Print summary

```
Package ready: Applications/{CompanyName}_{date}/

Mode: SHORT (default) — PDF only, EN CV only
Files created:
  - Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026.pdf
  - Cover_Letter_EN.pdf
  - Cover_Letter_PL.pdf

(With --full flag: adds DOCX files, PL CV, About_Me_EN.txt, About_Me_PL.txt)

ATS keywords matched: [list 8-10 from job that are in resume]

ATS Gap Report:
  Added to resume:   [skill1, skill2, ...] - plausible additions
  To learn/improve:  [skill1, skill2, ...] - genuinely missing, worth studying
  Skipped:           [skill1] - too far from profile

Stack: {Stack} | Language: {EN/PL}
```

---

## Ihar's full background (use this - do NOT read any files)

**Contact**: Ihar Petrasheuski (also known as Igor Pietraszewski)
+48 571 525 110 | igrflex@gmail.com | linkedin.com/in/ijerweb | Wrocław, Poland

**Core stack**: Angular (2-21), NgRx, RxJS, Signals, Nx Monorepo, AG Grid, TypeScript, JavaScript, HTML, Bootstrap, SCSS
**Tools**: Jest, Jasmine, Cypress, Git, Jenkins, Webpack, Node.js
**Methodologies**: Agile (Scrum, SAFe), Frontend Architecture, Code Reviews, Performance Optimization, CI/CD
**Languages**: English (Fluent), Russian (Native), Polish (B1 Intermediate)

**Total experience**: 10+ years (since Nov 2015). ALWAYS use "10+" - never round down to 9+ or 8+.

**Work Experience**:

**Senior Frontend Developer (Angular) | Fairmarkit (via contractor)** - Jun 2025 - March 2026
AI-powered Enterprise Procurement Platform | USA (Global)
- Contributed to frontend development of an AI-powered procurement platform serving enterprise clients globally, built with Angular 19 in an Nx monorepo.
- Delivered two domain-specific features covering complex procurement workflow logic, including one feature with direct AI integration; built an automation dashboard improving procurement workflow visibility.
- Worked extensively with Angular Signals, NgRx state management, and AG Grid for complex heavy data tables in a production environment.
- Participated in frontend architecture decisions within a cross-functional team of ~10, part of a ~200-person engineering organization.
- Maintained code quality through regular code reviews in Agile (Scrum).
Stack: Angular 19, TypeScript, Signals, RxJS, NgRx, Nx Monorepo, AG Grid, SCSS.

**Senior Frontend Developer (Angular) | Venture Labs** - July 2023 - April 2025
Banking Sector | Carbon Footprint Calculations | Poland | Client: Atruvia AG - core banking IT provider for 300+ German cooperative banks
- Built two Angular applications from scratch, actively used by 300+ German banks.
- Provided ongoing support and feature development for a third critical application.
- Migrated projects across Angular versions (14 → 19) ensuring code quality and minimal downtime.
- Ensured high code quality through unit tests, E2E tests, and integration with SonarQube.
- Designed and maintained Jenkins pipelines for automated builds, tests, and deployments.
- Conducted regular code reviews and worked in a cross-functional Agile team (10+ members).
Stack: Angular 14-19, TypeScript, SCSS, RxJS, NgRx, Java (backend).

**Senior Frontend Developer (Angular) | SII** - November 2022 - July 2023
Finance Sector | Financial Instruments Management
- Developed new frontend features and modules, led Angular version upgrades.
- Participated in architecture discussions, worked closely with backend, analysts, and QA in Agile.
Stack: Angular 10-12, TypeScript, SCSS, RxJS, NgRx, AG Grid, Java (backend). Team: 10+ members.

**Senior Frontend Developer (Angular) | Altoros** - April 2018 - November 2022
E-commerce | Insurance | Healthcare
- E-commerce: Built and scaled an advanced platform with a powerful admin panel. Stack: Angular 11-14, TypeScript, SCSS, Node.js.
- Healthcare (British Hospital): Inherited unfinished app, completed, optimized and stabilized it. Stack: Angular 11-14, RxJS, AG Grid, .NET.
- Insurance: Built real-time incident management with live maps and SignalR. Stack: Angular 6-8, AG Grid.

**Frontend Developer (Angular) | SolbegSoft** - April 2016 - April 2018
Maintenance Services Management - Developed a task management platform for service engineers; collaborated with BE and QA in Agile. Stack: Angular 2-6, TypeScript, SCSS, Bootstrap. Backend: .NET.

**Frontend Developer | Staronka** - November 2015 - March 2016
Startup | Website Builder - Worked on the core website-building tool; focused on responsive layouts and UI fixes. Stack: AngularJS, JavaScript, SCSS.

**Education**: Belarusian State Technological University - Bachelor, PE and Systems of Information Processing

**Additional Courses**: Angular Updates Course, Angular Advanced Course, Angular Core Course, JS Architecture Workshop, RxJS Course, Java basic Course, Node.js Course, JavaScript Advanced Level
