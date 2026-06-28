# DeepSeek R1 via OpenRouter — implementation plan

**Branch:** `feat/deepseek-provider`
**Goal:** cut per-vacancy LLM cost (~$0.50 today on Sonnet 4.6) by adding **OpenRouter**
as a third LLM provider, with **`deepseek/deepseek-r1`** as the first model we route to.
**Non-goal:** replace Anthropic wholesale. Sonnet stays as default and fallback; the
claim-judge keeps Haiku.

---

## Why OpenRouter (not direct DeepSeek)

DeepSeek's own billing is China-hosted, prepaid, with patchy card support outside Asia.
OpenRouter solves this:

- One account, one balance, **European/Stripe billing** (standard card).
- OpenAI-compatible API — we reuse the existing `openai` Python SDK, just point at
  `https://openrouter.ai/api/v1`.
- Same key + same code path will later let us A/B Gemini, Qwen, GPT-4.1, etc. without
  any new integration work — just change the `LLM_MODEL` string.
- Markup is ~5–10% over the model's native rate. On our projected $0.07/vacancy that's
  ~$0.005 extra — irrelevant vs the infra headache of a Chinese billing account.

Trade-off accepted: tiny markup in exchange for normal billing + optionality on future
models.

## Why DeepSeek R1 first

| Model (OpenRouter id) | Input $/1M | Output $/1M | Notes |
|---|---|---|---|
| `anthropic/claude-sonnet-4.6` (current) | ~3.00 | ~15.00 | baseline |
| **`deepseek/deepseek-r1`** | **~0.55** | **~2.19** | reasoning, JSON-mode OK |
| `deepseek/deepseek-chat` | ~0.27 | ~1.10 | cheaper, non-reasoning, follow-up |

(Verify against https://openrouter.ai/models before merge — OpenRouter shows live rates
incl. their markup.)

Per-vacancy projection on 8 calls: **$0.50 → ~$0.07–0.10**.

R1 first (not V3) because:
- ATS keyword-mirroring + claim-honest CV rewriting benefit from a reasoning model — we
  already saw fabrication regressions on weaker generators (Phase C claim-judge exists
  *because* of that). R1's chain-of-thought reduces that risk vs V3.
- Output tokens dominate our cost (CV + cover letter generation); R1 output is ~7× cheaper
  than Sonnet — still a big win even at R1's premium over V3.
- If R1 quality is acceptable we revisit V3 as follow-up for the cheaper sub-tasks
  (ATS rewrite loop is the biggest token sink and probably fine on V3).

## API shape

OpenRouter is **OpenAI-compatible**:
- `base_url`: `https://openrouter.ai/api/v1`
- SDK: existing `openai` Python package, just point it at the OpenRouter base URL.
- Auth: `Authorization: Bearer <OPENROUTER_API_KEY>`.
- Model id: `deepseek/deepseek-r1` (vendor-prefixed; OpenRouter convention).
- JSON mode: `response_format={"type": "json_object"}` works (note: requires the word
  "json" somewhere in the prompt — generation_rules.md already says so).
- Optional headers OpenRouter recommends (for rate-limit fairness + attribution, not
  required): `HTTP-Referer` and `X-Title`. We'll set `X-Title: job-hunter-bot`.
- Usage shape: OpenRouter forwards the underlying provider's `usage` block in OpenAI
  format — `prompt_tokens`, `completion_tokens`, plus DeepSeek-specific
  `prompt_cache_hit_tokens` / `prompt_cache_miss_tokens` when available. Defensive
  reads — missing fields default to 0.
- Reasoning output: for R1, OpenRouter exposes the reasoning trace as
  `message.reasoning` (separate from `message.content`); we want **`.content` only**
  for JSON parsing. No need to opt out of reasoning — it just goes to a separate
  field we ignore.

## Data-residency note

Routing through OpenRouter (US-hosted) → DeepSeek (China-hosted). The CV pipeline
ships `candidate_profile.md` (name, contacts, employment history) to whichever provider
serves the request. This is the user's own personal data being sent to generate the
user's own CV — not third-party PII — so GDPR-wise it is a choice the user makes for
themselves, not a compliance break. Documented in `.env` example so the choice is
explicit.

---

## Implementation plan (one PR, small commits)

### Step 1 — `llm_client.py`: add `_call_openrouter`

- New provider branch in `call_llm`: `provider == "openrouter"` → `_call_openrouter`.
- `_call_openrouter(system, user, model, key, max_tokens)`:
  - Reuse the `openai` SDK:
    ```python
    openai.OpenAI(
        api_key=key,
        base_url="https://openrouter.ai/api/v1",
        default_headers={"X-Title": "job-hunter-bot"},
    )
    ```
  - `chat.completions.create(model=..., messages=[...], max_tokens=..., response_format={"type":"json_object"})`.
  - Return `response.choices[0].message.content` (NOT `reasoning`).
  - Map `usage` to the anthropic-shaped record:
    - `input_tokens` = `prompt_tokens - prompt_cache_hit_tokens` (defensive: clamp ≥0)
    - `cache_read_input_tokens` = `prompt_cache_hit_tokens` (0 if absent)
    - `cache_creation_input_tokens` = 0 (DeepSeek doesn't expose writes via OpenRouter)
    - `output_tokens` = `completion_tokens`
  - `RateLimitError` → `LLMRateLimitError`; 429/5xx → retry (existing logic catches it).
- No `effort` / `thinking` / `cache_control` — those are Anthropic-specific. Don't pass
  them through.
- No new dependency: `openai` is already in `requirements.txt`.

### Step 2 — `hunter/llm_cost.py`: add DeepSeek pricing

- Add to `PRICING` (substring keys — `_resolve_pricing` longest-match wins):
  ```python
  "deepseek-r1":   {"input": 0.55, "output": 2.19, "cache_write": 0.55, "cache_read": 0.14},
  "deepseek-chat": {"input": 0.27, "output": 1.10, "cache_write": 0.27, "cache_read": 0.07},
  ```
- Note: OpenRouter passes the underlying provider's rate plus markup. We bake in
  approximate listed rates here for telemetry. The Anthropic Console comparison check
  (existing user habit) becomes an OpenRouter dashboard check instead — our number is
  an estimate, not the source of truth. Documented in the file's module docstring.
- Model id arriving as `deepseek/deepseek-r1` — substring `deepseek-r1` still matches.
  No prefix-stripping needed.

### Step 3 — `hunter/config.py`: provider plumbing

- `LLM_PROVIDER` already env-driven — no schema change. Accept `"openrouter"`.
- Extend `LLM_API_KEY` fallback:
  ```python
  LLM_API_KEY: str = (
      os.getenv("LLM_API_KEY", "")
      or os.getenv("ANTHROPIC_API_KEY", "")
      or os.getenv("OPENROUTER_API_KEY", "")
  )
  ```
- Add to `.env.example` / CLAUDE.md config table:
  ```
  LLM_PROVIDER=openrouter
  LLM_MODEL=deepseek/deepseek-r1
  LLM_API_KEY=sk-or-v1-...
  ```

### Step 4 — Tests

New file `tests/test_llm_client_openrouter.py`:
- `_call_openrouter` happy path (mock the `openai` SDK, assert `base_url`,
  `response_format`, model id, `X-Title` header passed through).
- Usage mapping: `prompt_cache_hit_tokens` → `cache_read_input_tokens`,
  `prompt_tokens − hit` → `input_tokens`, clamp to ≥0 when fields missing.
- `RateLimitError` → `LLMRateLimitError`.
- `_parse_json` still works on R1 output (it strips fenced blocks; we read `.content`
  not `.reasoning`, so reasoning text never reaches the parser).

New tests in `tests/test_llm_cost.py`:
- `_resolve_pricing("deepseek/deepseek-r1")` → R1 rates (not Sonnet fallback).
- `_resolve_pricing("deepseek/deepseek-chat")` → V3 rates.
- `usd_for_call` arithmetic on a known R1 usage record.

### Step 5 — Preview run (`tools/preview_apply.py`)

- Run apply pipeline against `tests/fixtures/sample_jobs/`:
  ```
  LLM_PROVIDER=openrouter LLM_MODEL=deepseek/deepseek-r1 \
      python tools/preview_apply.py <fixture>
  ```
- Cover at least one PL and one EN posting (ideally one per track:
  angular / react / ai / fullstack_*).
- Capture: content.json, full pipeline cost summary, total wall-time.
- Manually diff content.json vs Sonnet baseline for:
  - Polish/English contamination (`hunter.lang_guard` enforce-gate must still pass).
  - Claim fabrications (does the existing judge catch them, or does R1 hallucinate
    differently?).
  - JSON validity (R1 sometimes prepends prose — confirm `_parse_json` handles it).
  - ATS keyword mirroring quality.
- Record findings in this doc under "Verification results" before merging.

### Step 6 — Docs

- Update `CLAUDE.md` config table: list `openrouter` as `LLM_PROVIDER` option, with
  example model id.
- Add an Agent Work Log entry.
- Don't change the default — Sonnet stays default until R1 has a verification track
  record. Switching is one env var, no migration.

---

## Risk + rollback

- **JSON-format drift** — R1 reasoning models occasionally prepend a sentence before
  the JSON. `_parse_json` already handles this (scans for `{`, `raw_decode`s).
  Mitigation: `response_format={"type":"json_object"}` enforced strictly.
- **Polish quality** — unknown until preview run. Mitigation: `claim_judge` and
  `lang_guard` enforce-gate stay on; they catch regressions automatically. If R1
  breaks PL CVs, fall back to Sonnet for PL-detected postings (route by
  `primary_lang`) — not in v1, but easy to add.
- **OpenRouter outage / model deprecation** — single-vendor risk. Rollback is one env
  var flip back to `LLM_PROVIDER=anthropic`. OpenRouter itself can also re-route a
  deprecated DeepSeek snapshot to a current one if needed.
- **Rate limits / 429** — OpenRouter has both account-level and per-provider limits.
  Existing retry/backoff (`_RETRYABLE = {429, 500, 502, 503, 529}`) covers it.
- **Latency** — R1 reasoning is slower than Sonnet (extra tokens for the hidden CoT).
  Acceptable for a batch CV-generation pipeline; flagged for measurement in Step 5.
- **Cost telemetry drift** — our `llm_cost.py` numbers are estimates. The OpenRouter
  dashboard is source of truth; reconcile occasionally.

---

---

## Architecture: provider switching (phase B)

The point of adding OpenRouter is **not to replace Sonnet** but to make the generator
LLM a runtime choice — Sonnet stays, DeepSeek joins, more options follow. The user
expects a Telegram "button" (`/llm <name>`), not an env-var + restart.

### Design

**LLM profile** = a named (provider, model, api_key) triple. Defined once in config,
selected at runtime. Existing `(LLM_PROVIDER, LLM_MODEL, LLM_API_KEY)` is collapsed
into one of these profiles — no breaking change.

```python
# hunter/llm_profiles.py
PROFILES = {
    "sonnet":      Profile("anthropic",  "claude-sonnet-4-6",       env="ANTHROPIC_API_KEY"),
    "deepseek-r1": Profile("openrouter", "deepseek/deepseek-r1",    env="OPENROUTER_API_KEY"),
    "deepseek-v3": Profile("openrouter", "deepseek/deepseek-chat",  env="OPENROUTER_API_KEY"),
    # gemini-flash, qwen, gpt-4.1 land here as one-line additions
}
```

A profile is **available** if its `env` key resolves to a non-empty value. `/llm` only
offers available profiles — no dead options in the UI.

### Active-profile state

- **Source of truth:** a row in `tracker.db` (`config` table, key `active_llm_profile`)
  so the choice survives container restart.
- **Default:** `LLM_DEFAULT_PROFILE` env var → fallback `sonnet`. Existing `.env`
  setups (`LLM_PROVIDER=anthropic`/`LLM_MODEL=claude-sonnet-4-6`) keep working — they
  resolve to the `sonnet` profile.
- **Reload semantics:** `get_active_profile()` reads the DB row each apply cycle —
  no in-memory caching of the choice, so `/llm` takes effect on the next vacancy
  without a restart. The apply pipeline is the only consumer; one DB read per
  vacancy is free.

### Resolution layer

`apply_api` / `apply_cli` stop reading `config.LLM_PROVIDER` / `config.LLM_MODEL` /
`config.LLM_API_KEY` directly. Instead:

```python
profile = llm_profiles.get_active()  # → Profile(provider, model, api_key)
content = call_llm(system, user, provider=profile.provider,
                   model=profile.model, api_key=profile.api_key)
```

The judge is **not** in this pool — it stays on its own `JUDGE_MODEL` (Haiku) because
its job is independent verification. Cross-provider judging is a future optimisation,
not part of this design.

### Telegram `/llm` command

- `/llm` → show current profile + list available ones + (rough) per-vacancy cost
  estimate per option.
- `/llm <name>` → switch active profile. Validate the env key is present; refuse if
  not, with a hint pointing at `.env`.
- Owner-only (existing `TELEGRAM_CHAT_ID` check, reused from other commands).

### Per-role routing — explicitly deferred

Generator / ATS-rewrite-loop / cover-letter-review / judge could each pick a
different model (e.g. Sonnet for generator, DeepSeek V3 for the cheap ATS loop,
Haiku for judge). The profile registry is forward-compatible with this — we'd add
`ROLE_PROFILES = {"generator": ..., "ats_loop": ..., "judge": ...}`. **Not in this
PR.** Reasons:

1. We don't yet know empirically whether R1 is good enough for the whole pipeline —
   preview run (Step 5) tells us. If R1 ships everything cleanly, per-role routing
   is premature.
2. Multi-model pipelines are a debugging nightmare when judge findings disagree with
   generator output. One-model-per-vacancy keeps the failure mode simple.
3. The structural change (single `get_active()` → role-keyed lookup) is one file's
   worth of churn we can do later without breaking the user-facing `/llm`.

### Phasing

- **Phase A (this PR):** OpenRouter provider + DeepSeek R1 model, switched via env
  var only. Preview-validated. Sonnet still default. **Proves the provider works.**
- **Phase B (next PR):** profile registry + `/llm` command + DB-persisted active
  profile. **No new LLM code** — just wraps what A built. Easier to review.
- **Phase C (later, if needed):** per-role routing, more model profiles
  (Gemini / Qwen / GPT-4.1).

Splitting A from B keeps each PR small and means we don't build a switcher before we
have something worth switching to.

---

## Out of scope (follow-ups)

- **Phase B** above (separate PR after A merges and preview run is clean).
- **Phase C** above (separate PRs per model family).
- Per-role model routing (judge vs generator vs ATS-loop on different providers).
- Claude CLI subscription path (`APPLY_USE_CLI=true`) — untouched; this PR is API-only.
- Default-profile switch — only after preview run + low-volume real applies.
- Direct DeepSeek provider (bypass OpenRouter for the 5–10% saving) — not worth it
  unless we hit OpenRouter-specific issues.

---

## Verification results

Verified 2026-06-28 on `tests/fixtures/sample_jobs/angular.txt` (Lumicode PL posting).

- [x] Preview run on `tests/fixtures/sample_jobs/angular/` — PL posting, both EN+PL CVs generated
- [x] `lang_guard` enforce-gate clean — no Polish in EN resume (✅ QA check passed)
- [x] Content QA all green — 7/7 roles, companies match, titles match, no duplicate Angular
- [x] `claim_judge` routing fixed — judge correctly calls Anthropic (not OpenRouter); model ID mismatch was the bug, fixed via JUDGE_PROVIDER/JUDGE_API_KEY
- [x] **Measured cost: $0.0851/vacancy** (7 calls, deepseek/deepseek-r1) vs ~$0.50 Sonnet → **~6× cheaper**
- [x] Wall-time: ~3-4 min/vacancy (R1 reasoning overhead — acceptable for batch pipeline)
- [x] ATS score: 98% with 100% keyword match
- [ ] Anthropic credit balance low — judge skipped in run 2 (unrelated to this PR; top up console.anthropic.com)

---

## Sign-up checklist (one-time, for the user)

1. Register at https://openrouter.ai.
2. Top up a balance ($5 is plenty for tests — covers ~50–70 real R1 vacancies).
3. Create an API key (`sk-or-v1-...`) at https://openrouter.ai/settings/keys.
4. Add to `.env`:
   ```
   LLM_PROVIDER=openrouter
   LLM_MODEL=deepseek/deepseek-r1
   LLM_API_KEY=sk-or-v1-...
   ```
5. Bot restart — done.
