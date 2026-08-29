---
description: Generate a complete tailored application package (resume, cover letters, about-me, ATS report) for one job posting, using this repo's own prompts, candidate profile and generate_docs.py.
argument-hint: <job URL or pasted job text> [--full]
---

You are helping the candidate apply for a frontend developer job. Generate a complete tailored application package.

Repo files below (`prompts/`, `generate_docs.py`) are relative to the repository root — run this command from there; the CLI pipeline does exactly that (`hunter/apply_cli.py` spawns `claude -p "/apply …"` with `cwd=PROJECT_DIR`). The candidate's personal files and the output folder are NOT in the repo — resolve them from the environment as shown in Steps 1 and 3.

## Input
$ARGUMENTS

---

## Non-negotiable: decide, never ask

This command runs non-interactively (`claude -p`, spawned by
`hunter/apply_cli.py`). **Nobody is reading your output while it runs and nobody
can answer a question.** A clarifying question does not pause the run — it ends
it: the process produces no output folder, `main_cli` raises, and the vacancy
comes back with an alert. Measured on the deploy host 2026-08-24: 5 of 60
retained runs died exactly this way, each burning the full 600 s timeout and
arming an hour-long auto-apply pause, for questions like *"Do you want me to
proceed anyway, or should I skip this one?"*.

So: **generate the package, always.** You are not the gate. Every deterministic
screen the project has — expired check, doomed gate (location, work
authorization, language, foreign stack), re-post gate, React-only and
backend-only pre-LLM checks — already ran and passed this vacancy before you
were started, and the post-generation gates (stack, company+title dedup, claim
judge, language gate) run after you and abort cleanly on their own. Deciding
again in here can only produce a run that cost the full generation and left
nothing behind.

If the vacancy looks like a poor fit — wrong stack, wrong seniority, on-site in
the wrong city, a language the candidate does not have — say so plainly in your
Step 6 summary, in one or two sentences, and generate it anyway. That note is
read: it lands in `logs/apply_stdout/` and in the Telegram message, and it is
how a missing gate rule gets found. Silence is what costs money here, not
candour.

The single exception is a genuinely missing input — no candidate profile, an
unreadable posting — which Step 1 and Step 2 already tell you to stop on. Stop
means stop with a clear one-line reason, not a question.

---


---

## Step 1 - Load generation rules and base CV

Load the generation rules by running this command and using its ENTIRE stdout as your instructions — do NOT read `prompts/generation_rules.md` directly, its personal-facts placeholder is only meaningful once rendered:

```bash
python -m hunter.gen_prompt
```

This prints the tracked, employer-agnostic rules PLUS the active candidate's own employment facts (real employer table, backend-per-role rules, years of experience, track-title overrides) rendered from `candidate.yaml` at call time — the API pipeline (`hunter/apply_api.py`) builds the exact same text from the exact same renderer (`hunter/gen_prompt.py`), so both pipelines see byte-identical rules for the same candidate. It covers ATS gap analysis, red lines, resume structure, cover letter spec (two-layer model, quality gates), about me, ATS scoring loop, and output JSON schema.

The candidate's own files are NOT at a fixed path — since the multi-user migration they live under `users/{userId}/candidate/`, and the active user is named by `CANDIDATE_YAML_PATH` (injected per user by `hunter.users.user_env`). Resolve the directory first, in one command:

```bash
CAND_DIR="$(dirname "${CANDIDATE_YAML_PATH:-candidate/candidate.yaml}")" && echo "Using: $CAND_DIR"
```

Use the value printed above as `{cand_dir}` below. Unset (a plain local checkout) falls back to `candidate/`; in the container it resolves to `/app/users/{userId}/candidate`.

Read the candidate profile from `{cand_dir}/candidate_profile.md` — the single source of truth for all candidate data. It is gitignored (personal); if it is missing, stop and tell the user. `candidate_profile.example.md` is a placeholder template, never a substitute — generating from it would produce a CV for a fictional person.

Also load the base-CV stack map (stack key -> filename), so an override in `candidate.yaml`'s `tracks.base_cv` is honored instead of a hardcoded guess:

```bash
python -m hunter.gen_prompt base-cv-map
```

Output lines look like `angular=base_cv_angular.md`. After reading the job posting (Step 2), detect the primary stack with these heuristics and load `{cand_dir}/{filename}` using the filename the map above gives for that key:
- AI-first / LLM / Agentic roles → key `ai`
- React + Next.js / NestJS (React prominent) → key `fullstack_react_next`
- Angular + NestJS / Full-Stack (Angular or NestJS alone) → key `fullstack_angular_nest`
- Angular → key `angular`
- React / Next.js / JavaScript → key `react`

Use the base CV as a starting point for experience bullets and skills order. Follow the "Base CV" instructions in the generation rules loaded above.

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
- If fetching fails or returns CSS/empty content: **stop** with the one-line reason `could not read the posting` and generate nothing. Do NOT ask a question (nobody is there to answer, see the rule at the top) and do NOT write a package from the URL alone — with no posting text the pipeline's own screens (expired check, doomed gate, re-post gate, ATS verdict) are all skipped, and the claim judge has nothing to check the CV against, so an invented vacancy would sail through to delivery. The pipeline aborts on a too-short posting before it ever spawns you; stopping here is the same decision one step later.

If input is plain text: use it directly. It may be followed by one or more clearly-labeled deterministic instruction blocks (e.g. a `## ATS keyword checklist` section, or a `**Language optimization:**` note) that the apply pipeline appended after the job posting text before starting you — the SAME additions the API pipeline computes for the same posting. Treat those as generation instructions for Step 4, not as part of the job posting itself.

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

Follow all rules from the generation rules loaded in Step 1 to produce the full application package: resume EN, cover letter EN+PL, about me EN+PL, ATS analysis.

One difference from the API pipeline: set `"resume_pl": null` by default — it saves the output tokens of a Polish CV nobody receives. Two exceptions where you MUST populate it with a full Polish-translated resume:

- **the job posting itself is written in Polish** — a Polish employer receives the Polish CV as the primary document, so it ships even in the default short flow;
- `--full` is explicitly passed as an argument.

(If it comes back empty on a Polish posting anyway, `apply_shared.ensure_pl_resume` mirrors it from `resume_en` — but that costs an extra translation call, so get it right here.)

---

## Step 5 - Write content.json and run the generator

⚠️ Save content.json INSIDE the output folder, not in the project root.
⚠️ Do NOT write any Python scripts or create any .py files.

Write to `{base_dir}/{YYYY-MM-DD}/{CompanyName}/content.json` (using the same `{base_dir}` from Step 3).

The JSON schema is defined in the generation rules loaded in Step 1. Additionally include these workflow fields:

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
