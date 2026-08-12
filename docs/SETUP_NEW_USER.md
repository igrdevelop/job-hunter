# Setup for a new user

This is the step-by-step path from a fresh clone of this repo to generating
your first tailored CV. Two paths are covered:

- **Quick start (CLI only)** — generate a CV for a single vacancy URL.
  No Telegram bot, no API key — just a Claude Pro subscription.
- **Full setup** — the Telegram bot + automated hunting across 25 job boards.

See [docs/CANDIDATE_YAML_PLAN.md](CANDIDATE_YAML_PLAN.md) for the design
rationale behind `candidate.yaml`.

---

## Quick start: CLI only (Claude Pro, no Telegram)

If you have a Claude Pro/Max subscription and just want to try the pipeline
on a single vacancy, this is the fastest path. No Telegram bot, no API key.

### 1. Clone and install

```bash
git clone https://github.com/igrdevelop/job-hunter.git
cd job-hunter
pip install -e .
```

### 2. Install LibreOffice

Required for PDF rendering. Install from
[libreoffice.org](https://www.libreoffice.org/download/download/).

On Windows, add to your `.env`:

```
SOFFICE_PATH=C:/Program Files/LibreOffice/program/soffice.exe
```

On Linux/Mac, `libreoffice` on PATH is usually enough.

### 3. Log in to Claude CLI

```bash
claude
```

Follow the OAuth prompt to authenticate with your Claude Pro account.

### 4. Configure your candidate data

Edit the files in `candidate/` with your real data — see
[candidate/README.md](../candidate/README.md) for what each file does:

1. **`candidate/candidate.yaml`** — name, city, languages, employers
2. **`candidate/candidate_profile.md`** — free-text career history
3. **`candidate/base_cv_angular.md`** (or your track) — pre-polished resume bullets
4. **(Optional) `candidate/filters.yaml`** — copy from `filters.example.yaml`
   to tune what the bot hunts for (stack, levels, hybrid rules). No file =
   shared defaults. Changes apply on the next `/hunt`.

**This step is not optional.** The `.example` files are templates, not
fallbacks — nothing loads them automatically. Without your own
`candidate.yaml`, document generation aborts with a message naming the
missing fields (`hunter/candidate.py::require_identity`), and without
`candidate_profile.md` the LLM has no career history to tailor from.

### 5. Generate a CV

```bash
python apply_agent.py --cli "https://nofluffjobs.com/pl/job/some-position"
```

### 6. Find your documents

Generated documents are saved to:

```
Applications/{date}/{CompanyName}/
  content.json          # structured LLM output
  job_posting.txt       # fetched job description
  CV_*.docx / .pdf      # tailored resume
  Cover_Letter_*.docx / .pdf
  judge_report.json     # claim verification results
  outreach.md           # recruiter contact + LinkedIn message draft
```

The `Applications/` folder is in the project root (gitignored). Override
with `APPLICATIONS_DIR` in `.env`.

You can also test with bundled fixtures (no real URL needed):

```bash
python tools/preview_apply.py --track angular
```

---

## Full setup: Telegram bot + automated hunting

This gives you the full experience: automated scraping of 25 job boards,
Telegram notifications with Apply/Skip buttons, scheduled hunts, and
tracking in SQLite (with optional Google Sheets/Drive mirror).

### 1. Clone and install

```bash
git clone https://github.com/igrdevelop/job-hunter.git
cd job-hunter
pip install -e .
```

(or `pip install -r requirements.lock` for the exact pinned versions CI and
Docker use)

### 2. Install LibreOffice

Install from [libreoffice.org](https://www.libreoffice.org/download/download/)
and confirm the path matches your install. On Windows, set `SOFFICE_PATH`
in `.env` (see step 5).

### 3. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   and follow the prompts to get a bot token.
2. Message your new bot once (anything), then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find your
   numeric `chat.id` — that's your `TELEGRAM_CHAT_ID`.

### 4. Configure your candidate data

All candidate-personal files live in the `candidate/` folder. Edit them
with your real data — see [candidate/README.md](../candidate/README.md)
for what each file does.

The three files to edit (plus an optional fourth for hunt policy):

1. **`candidate/candidate.yaml`** — structured identity: name, city,
   languages, employers. Drives filters, QA checks and LLM prompts.
2. **`candidate/candidate_profile.md`** — free-text career history.
   The LLM reads this + the job posting to generate your CV.
3. **`candidate/base_cv_angular.md`** (or whichever track you target) —
   pre-polished resume bullets. Dates and companies must match
   `candidate_profile.md`.
4. **(Optional) `candidate/filters.yaml`** — job-intake policy (title
   keywords, stack exclusions, hybrid rules, …). Copy from
   `candidate/filters.example.yaml`. Missing file = shared defaults
   (today's Angular/Wrocław owner behavior). Edit + `/hunt` — no
   deploy needed. See [docs/FILTERS_YAML_PLAN.md](FILTERS_YAML_PLAN.md).

**This step is not optional.** The `.example` files are templates you copy
and edit — nothing loads them automatically. Hunting, filtering and Telegram
work without them, but the moment a document would be rendered the run
aborts with a message naming the missing identity fields. That is
deliberate: the alternative is mailing a real employer a CV with a
placeholder name on it.

### 5. Configure environment variables

```bash
cp .env.example .env
```

Fill in the required variables:

- `TELEGRAM_BOT_TOKEN` — from step 3
- `TELEGRAM_CHAT_ID` — from step 3

For LLM, choose one of:

| Option | What to set |
|--------|-------------|
| Anthropic API key | `ANTHROPIC_API_KEY=sk-ant-...` |
| OpenAI API key | `LLM_PROVIDER=openai` + `OPENAI_API_KEY=sk-...` |
| OpenRouter API key | `LLM_PROVIDER=openrouter` + `OPENROUTER_API_KEY=sk-or-...` |
| Claude Pro subscription (no API key) | `APPLY_USE_CLI=true` — and run `claude` to log in first |

Everything else in `.env.example` has a working default; tune it later.

### 6. Start the bot

```bash
python hunter.py
```

Message your bot `/start` in Telegram to confirm it's alive.

### 7. Run your first hunt

```
/hunt justjoin
```

This scrapes a single source (JustJoin.it) so you can verify filtering and
(if `AUTO_APPLY=true`) generation work end-to-end before turning on the full
25-source schedule. Check `/status` and `/health` afterwards.

### Where are the generated documents?

Same as CLI mode — in `Applications/{date}/{CompanyName}/` relative to the
project root (gitignored). Override with `APPLICATIONS_DIR` in `.env`.

## Optional: Google Sheets / Drive / Gmail

Each of these needs its own one-time OAuth setup — see the "Google Sheets
Setup" and related sections in [CLAUDE.md](../CLAUDE.md). Skip them for a
first run; the bot works fully offline (Telegram + local `tracker.db`)
without them.
