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

## 6. Configure your identity: candidate.yaml

```bash
cp candidate.example.yaml candidate.yaml
```

Fill in:

- `identity` — your name, headline, CV filename prefix, and the contact line
  printed on the generated CV.
- `location` — your home city (and lowercase aliases), which cities you'll
  accept a hybrid commute to, and your work authorization (`EU`/`US`/`any`).
  This drives the job-board location filters and the doomed-vacancy gate.
- `languages` — which languages you speak, which CV language variants to
  generate, and which REQUIRED languages in a posting should auto-skip it
  (defaults to German/French/Dutch — drop a code if you speak that language).
- `employers` — your real employer names (used by content QA and the
  contamination guard so a company name is never mistaken for a foreign-
  language leak), your canonical profile titles, and one optional "flexible"
  employer whose project list the verdict-refine loop may lightly extend on
  its last, most aggressive rewrite round (never invents an employer).
- `education` — your school (substring match, lowercase) and how many roles
  your resume should list.
- `source_urls` — only needed if you want Pracuj.pl / theprotocol.it /
  JobLeads to search a specific city instead of your `location.home_city`.

`candidate.yaml` is gitignored — it stays on your machine only. If you skip
this step entirely, the bot still runs using its original built-in defaults
(a warning is logged once); nothing crashes.

## 7. Configure your CV content

```bash
cp prompts/candidate_profile.example.md prompts/candidate_profile.md
cp prompts/base_cv_angular.example.md   prompts/base_cv_angular.md
```

Fill both in with your real work history, skills and projects. See
[prompts/README.md](../prompts/README.md) for the full system-vs-personal
file split and what each file is for.

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
