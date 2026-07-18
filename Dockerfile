FROM python:3.11-slim

WORKDIR /app

# nodejs/npm: for the Claude CLI (M4 outage fallback, docs/LLM_OUTAGE_RESILIENCE_PLAN.md).
# Debian bookworm ships Node 18 — the CLI's minimum.
RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/*

# Claude CLI (Pro subscription path). Only used when LLM_OUTAGE_FALLBACK_CLI=true
# AND a logged-in config dir is mounted (docker-compose: ./.claude-cli:/root/.claude);
# otherwise apply_cli._is_cli_available()'s login check keeps it out of the way.
RUN npm install -g @anthropic-ai/claude-code
# Keep ALL claude state (config + OAuth credentials) inside the mounted volume —
# without this the global config lands in /root/.claude.json OUTSIDE the mount
# and the login dies with the container.
ENV CLAUDE_CONFIG_DIR=/root/.claude
# The CLI refuses --dangerously-skip-permissions (which apply_cli.py passes) when
# running as root unless it is told it's in a sandbox. This container IS the
# sandbox: single-purpose, no interactive user.
ENV IS_SANDBOX=1

COPY requirements.lock pyproject.toml ./
RUN pip install --no-cache-dir -r requirements.lock
RUN playwright install chromium --with-deps

COPY . .
RUN pip install --no-cache-dir -e . --no-deps

RUN mkdir -p Applications backups

CMD ["python", "-m", "hunter"]