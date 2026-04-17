# AGENTS.md

See **[CLAUDE.md](./CLAUDE.md)** for full project context, architecture, pipeline description, and rules for agents.

## Quick orientation

- **Purpose:** Autonomous IT job hunter + CV generator bot (Telegram-controlled)
- **Stack:** Python 3.11+, python-telegram-bot, Anthropic/OpenAI API, openpyxl, playwright, cloudscraper
- **Entry point:** `python hunter.py` — starts Telegram bot + scheduler
- **Active branch:** `develop`
- **Config:** `hunter/config.py` reads from `.env` (never commit `.env`)
- **Tests:** `pytest tests/`
