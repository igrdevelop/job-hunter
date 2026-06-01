You are helping Ihar Petrasheuski apply for a frontend developer job. Generate a complete tailored application package.

## Input
$ARGUMENTS

---

## Step 1 - Load generation rules and base CV

Read the file `D:/LearningProject/Claude/prompts/generation_rules.md` — it is the single source of truth for all content generation rules: ATS gap analysis, red lines, resume structure, cover letter spec (two-layer model, story bank, quality gates), about me, ATS scoring loop, and output JSON schema.

Also read the candidate profile from `D:/LearningProject/Claude/prompts/candidate_profile.md` — use it as the single source of truth for all candidate data.

After reading the job posting (Step 2), detect the primary stack and load the matching base CV:
- AI-first / LLM / Agentic roles → `D:/LearningProject/Claude/prompts/base_cv_ai.md`
- React + Next.js / NestJS (React prominent) → `D:/LearningProject/Claude/prompts/base_cv_fullstack_react_next.md`
- Angular + NestJS / Full-Stack (Angular or NestJS alone) → `D:/LearningProject/Claude/prompts/base_cv_fullstack_angular_nest.md`
- Angular → `D:/LearningProject/Claude/prompts/base_cv_angular.md`
- React / Next.js / JavaScript → `D:/LearningProject/Claude/prompts/base_cv_react.md`

Use the base CV as a starting point for experience bullets and skills order. Follow the "Base CV" instructions in `generation_rules.md`.

---

## Step 2 - Get the job posting

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

## Step 3 - Create output folder

First, determine the base applications directory:
```bash
echo $APPLICATIONS_DIR
```

- If `APPLICATIONS_DIR` is set and non-empty → use it as the base
- Otherwise → use `D:/LearningProject/Claude/Applications`

Then create: `{base_dir}/{YYYY-MM-DD}/{CompanyName}/`

If a folder for this company already exists today, append `_2`, `_3`, etc.:
```
{base_dir}/{YYYY-MM-DD}/{CompanyName}_2/
```

Create the folder:
```bash
mkdir -p "{base_dir}/{date}/{CompanyName}"
```

---

## Step 4 - Generate content

Follow all rules from `generation_rules.md` (loaded in Step 1) to produce the full application package: resume EN, cover letter EN+PL, about me EN+PL, ATS analysis.

One difference from the API pipeline: set `"resume_pl": null` by default. Only populate it with a full Polish-translated resume when `--full` is explicitly passed as an argument.

---

## Step 5 - Write content.json and run the generator

⚠️ Save content.json INSIDE the output folder, not in the project root.
⚠️ Do NOT write any Python scripts or create any .py files.

Write to `{base_dir}/{YYYY-MM-DD}/{CompanyName}/content.json` (using the same `{base_dir}` from Step 3).

The JSON schema is defined in `generation_rules.md`. Additionally include these workflow fields:

```json
{
  "output_folder": "{base_dir}/{YYYY-MM-DD}/{CompanyName}",
  "apply_url": "the original input URL (or apply button URL if different)"
}
```

Then run the generator:

**Default (short mode)** — PDF only, EN CV only:
```bash
python D:/LearningProject/Claude/generate_docs.py "{base_dir}/{YYYY-MM-DD}/{CompanyName}/content.json"
```

**Full mode** (only when `--full` is explicitly passed):
```bash
python D:/LearningProject/Claude/generate_docs.py "{base_dir}/{YYYY-MM-DD}/{CompanyName}/content.json" --full
```

---

## Step 6 - Print summary

```
Package ready: Applications/{date}/{CompanyName}/

Mode: SHORT (default) — PDF only, EN CV only
Files created:
  - CV_{Stack}_2026_EN.pdf
  - Cover_Letter_EN.pdf
  - Cover_Letter_PL.pdf

ATS keywords matched: [list 8-10 from job that appear in resume]

ATS Gap Report:
  Added to resume:   [skill1, skill2, ...] - plausible additions
  To learn/improve:  [skill1, skill2, ...] - genuinely missing, worth studying
  Skipped:           [skill1] - too far from profile

Stack: {Stack} | Language: {EN/PL} | ATS Score: {score}%
```
