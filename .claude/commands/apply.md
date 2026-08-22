---
description: Generate a complete tailored application package (resume, cover letters, about-me, ATS report) for one job posting, using this repo's own prompts, candidate profile and generate_docs.py.
argument-hint: <job URL or pasted job text> [--full]
---

You are helping the candidate apply for a frontend developer job. Generate a complete tailored application package.

Repo files below (`prompts/`, `generate_docs.py`) are relative to the repository root — run this command from there; the CLI pipeline does exactly that (`hunter/apply_cli.py` spawns `claude -p "/apply …"` with `cwd=PROJECT_DIR`). The candidate's personal files and the output folder are NOT in the repo — resolve them from the environment as shown in Steps 1 and 3.

## Input
$ARGUMENTS

---

## Step 1 - Load generation rules and base CV

Read the file `prompts/generation_rules.md` — it is the single source of truth for all content generation rules: ATS gap analysis, red lines, resume structure, cover letter spec (two-layer model, story bank, quality gates), about me, ATS scoring loop, and output JSON schema.

The candidate's own files are NOT at a fixed path — since the multi-user migration they live under `users/{userId}/candidate/`, and the active user is named by `CANDIDATE_YAML_PATH` (injected per user by `hunter.users.user_env`). Resolve the directory first, in one command:

```bash
CAND_DIR="$(dirname "${CANDIDATE_YAML_PATH:-candidate/candidate.yaml}")" && echo "Using: $CAND_DIR"
```

Use the value printed above as `{cand_dir}` below. Unset (a plain local checkout) falls back to `candidate/`; in the container it resolves to `/app/users/{userId}/candidate`.

Read the candidate profile from `{cand_dir}/candidate_profile.md` — the single source of truth for all candidate data. It is gitignored (personal); if it is missing, stop and tell the user. `candidate_profile.example.md` is a placeholder template, never a substitute — generating from it would produce a CV for a fictional person.

After reading the job posting (Step 2), detect the primary stack and load the matching base CV from the same directory:
- AI-first / LLM / Agentic roles → `{cand_dir}/base_cv_ai.md`
- React + Next.js / NestJS (React prominent) → `{cand_dir}/base_cv_fullstack_react_next.md`
- Angular + NestJS / Full-Stack (Angular or NestJS alone) → `{cand_dir}/base_cv_fullstack_angular_nest.md`
- Angular → `{cand_dir}/base_cv_angular.md`
- React / Next.js / JavaScript → `{cand_dir}/base_cv_react.md`

Use the base CV as a starting point for experience bullets and skills order. Follow the "Base CV" instructions in `generation_rules.md`.

---

## Step 2 - Get the job posting

If input is a URL:
- **justjoin.it**: extract the slug from the URL and fetch via the candidate API:
  `https://justjoin.it/api/candidate-api/offers/{slug}`
  e.g. `https://justjoin.it/job-offer/syberry-senior-frontend-engineer-krakow-javascript`
  → slug = `syberry-senior-frontend-engineer-krakow-javascript`
  → fetch `https://justjoin.it/api/candidate-api/offers/syberry-senior-frontend-engineer-krakow-javascript`
  (this is the endpoint `hunter/sources/justjoin.py::fetch_text` uses — the old
  `api.justjoin.it/v1/offers/` host is dead)
- **All other URLs**: fetch the page directly with WebFetch.
- If fetching fails or returns CSS/empty content: ask the user to paste the job text manually.

If input is plain text: use it directly.

---

## Step 3 - Create output folder

Determine the base directory and create the output folder in ONE bash command — do NOT skip this step:
```bash
BASE_DIR="${APPLICATIONS_DIR:-$(pwd)/Applications}" && echo "Using: $BASE_DIR"
```

Use the value printed above as `{base_dir}` for all subsequent steps.

Then create: `{base_dir}/{YYYY-MM-DD}/{CompanyName}/`

If a folder for this company already exists today, append `_2`, `_3`, etc.:
```
{base_dir}/{YYYY-MM-DD}/{CompanyName}_2/
```

Create the folder:
```bash
mkdir -p "${APPLICATIONS_DIR:-$(pwd)/Applications}/$(date +%Y-%m-%d)/{CompanyName}"
```

---

## Step 4 - Generate content

Follow all rules from `generation_rules.md` (loaded in Step 1) to produce the full application package: resume EN, cover letter EN+PL, about me EN+PL, ATS analysis.

One difference from the API pipeline: set `"resume_pl": null` by default — it saves the output tokens of a Polish CV nobody receives. Two exceptions where you MUST populate it with a full Polish-translated resume:

- **the job posting itself is written in Polish** — a Polish employer receives the Polish CV as the primary document, so it ships even in the default short flow;
- `--full` is explicitly passed as an argument.

(If it comes back empty on a Polish posting anyway, `apply_shared.ensure_pl_resume` mirrors it from `resume_en` — but that costs an extra translation call, so get it right here.)

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

Then run the generator (use the same `{base_dir}` determined in Step 3):

**Default (short mode)** — PDF only, EN CV only:
```bash
python generate_docs.py "${APPLICATIONS_DIR:-$(pwd)/Applications}/$(date +%Y-%m-%d)/{CompanyName}/content.json"
```

**Full mode** (only when `--full` is explicitly passed):
```bash
python generate_docs.py "${APPLICATIONS_DIR:-$(pwd)/Applications}/$(date +%Y-%m-%d)/{CompanyName}/content.json" --full
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
