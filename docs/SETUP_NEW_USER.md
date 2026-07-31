# Setup for a new user

This is the step-by-step path from a fresh clone of this repo to your first
`/hunt` run, running the bot locally on your own machine (not the Docker VPS
deploy — see [docs/DEPLOY.md](DEPLOY.md) for that). No source code changes
are needed; everything candidate-specific lives in a few config files.

See [docs/CANDIDATE_YAML_PLAN.md](CANDIDATE_YAML_PLAN.md) for the design
rationale behind `candidate.yaml`.

## 1. Clone the repo

```bash
git clone https://github.com/igrdevelop/job-hunter.git
cd job-hunter
```

## 2. Install dependencies

```bash
pip install -e .
```

(or `pip install -r requirements.lock` for the exact pinned versions CI and
Docker use)

## 3. Install LibreOffice

The bot renders generated resumes/cover letters to PDF via LibreOffice
headless. Install it from [libreoffice.org](https://www.libreoffice.org/download/download/)
and confirm the path in `generate_docs.py` matches your install
(`C:/Program Files/LibreOffice/program/soffice.exe` on Windows by default).

## 4. Create a Telegram bot

1. Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   and follow the prompts to get a bot token.
2. Message your new bot once (anything), then visit
   `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser to find your
   numeric `chat.id` — that's your `TELEGRAM_CHAT_ID`.

## 5. Configure environment variables

```bash
cp .env.example .env
```

Fill in the three required variables at minimum:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `ANTHROPIC_API_KEY` (or another provider's key — see the `LLM_PROVIDER`
  table in [CLAUDE.md](../CLAUDE.md))

Everything else in `.env.example` has a working default; tune it later.

## 6. Configure your candidate data

All candidate-personal files live in the `candidate/` folder. Edit them
with your real data — see [candidate/README.md](../candidate/README.md)
for what each file does.

The three files to edit:

1. **`candidate/candidate.yaml`** — structured identity: name, city,
   languages, employers. Drives filters, QA checks and LLM prompts.
2. **`candidate/candidate_profile.md`** — free-text career history.
   The LLM reads this + the job posting to generate your CV.
3. **`candidate/base_cv_angular.md`** (or whichever track you target) —
   pre-polished resume bullets. Dates and companies must match
   `candidate_profile.md`.

If you skip this step, the bot runs using the example data that ships
with the repo (a warning is logged once); nothing crashes.

## 8. Start the bot

```bash
python hunter.py
```

Message your bot `/start` in Telegram to confirm it's alive.

## 9. Run your first hunt

```
/hunt justjoin
```

This scrapes a single source (JustJoin.it) so you can verify filtering and
(if `AUTO_APPLY=true`) generation work end-to-end before turning on the full
24-source schedule. Check `/status` and `/health` afterwards.

## Optional: Google Sheets / Drive / Gmail

Each of these needs its own one-time OAuth setup — see the "Google Sheets
Setup" and related sections in [CLAUDE.md](../CLAUDE.md). Skip them for a
first run; the bot works fully offline (Telegram + local `tracker.db`)
without them.
