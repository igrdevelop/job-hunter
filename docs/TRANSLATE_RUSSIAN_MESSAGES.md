# Task: Translate Russian Telegram Messages to English

## Goal

Translate all Russian-language **user-facing Telegram messages** in two files
to English. Functional Russian regex patterns (used for detecting Russian
content in job postings) and code comments quoting the owner stay untouched.

## Scope — exactly two production files + their tests

### 1. `hunter/best_effort.py` (2 strings)

| Line | Current (Russian) | Target (English) |
|------|-------------------|------------------|
| 184 | `f"⚠️ <b>{subsystem}</b>: {failures} подряд сбоев, последний: {str(e)[:200]}"` | `f"⚠️ <b>{subsystem}</b>: {failures} consecutive failures, latest: {str(e)[:200]}"` |
| 194 | `f"✅ <b>{subsystem}</b> восстановился"` | `f"✅ <b>{subsystem}</b> recovered"` |

### 2. `hunter/gmail_report.py` (~15 strings)

**`_REASON_LABELS` dict (lines 24-35):**

| Key | Current | Target |
|-----|---------|--------|
| `title_kw` | `"не по ключевым словам"` | `"keyword mismatch"` |
| `require_angular` | `"нет Angular"` | `"no Angular"` |
| `level` | `"уровень"` | `"level"` |
| `exclude_pattern` | `"стоп-стек"` | `"excluded stack"` |
| `react_no_angular` | `"React без Angular"` | `"React w/o Angular"` |
| `location` | `"локация"` | `"location"` |
| `russia` | `"работа в РФ"` | `"Russia-based"` |
| `german` | `"нужен немецкий"` | `"German required"` |
| `contract` | `"контракт/part-time"` | `"contract/part-time"` |
| `relocation` | `"релокация"` | `"relocation"` |

**Inline message strings:**

| Line | Current | Target |
|------|---------|--------|
| 92 | `подтверждение, пропущено` | `confirmation, skipped` |
| 96 | `0 ссылок (парсер не распознал)` | `0 URLs (parser miss)` |
| 108 | `дубл` | `dup` |
| 148 | `Gmail (по письмам)` | `Gmail (by email)` |
| 149 | `{total_emails} писем · {total_found} вакансий · взято <b>{total_taken}</b>` | `{total_emails} emails · {total_found} vacancies · taken <b>{total_taken}</b>` |
| 152 | `{zero_url} писем без распознанных ссылок (см. ниже)` | `{zero_url} emails with no parsed URLs (see below)` |
| 154 | `{skipped} писем-подтверждений пропущено` | `{skipped} confirmation emails skipped` |
| 157-158 | `достигнут потолок {max_results} писем — часть писем могла не попасть (подними GMAIL_MAX_RESULTS)` | `ceiling of {max_results} emails reached — some may be missing (raise GMAIL_MAX_RESULTS)` |

### 3. Tests to update

**`tests/test_best_effort.py`:**
- Line 107: `assert "восстановился" in sent[1]` → `assert "recovered" in sent[1]`

**`tests/test_gmail_report.py`:**
- Line 60: `"1 писем"` → `"1 emails"`
- Line 61: `"3 вакансий"` → `"3 vacancies"`
- Line 62: `"взято <b>1</b>"` → `"taken <b>1</b>"`
- Line 73: `"♻️ 1 дубл"` → `"♻️ 1 dup"`
- Line 80: `"React без Angular"` → `"React w/o Angular"`
- Line 87: `"0 ссылок"` → `"0 URLs"`
- Line 88: `"без распознанных ссылок"` → `"no parsed URLs"`
- Line 94: `"подтверждение, пропущено"` → `"confirmation, skipped"`
- Line 95: `"1 писем-подтверждений"` → `"1 confirmation emails"`
- Line 101: `"потолок 100"` → `"ceiling of 100"`

## Out of scope — do NOT touch

- Russian **regex patterns** in `hunter/filters.py`, `hunter/sources/telegram_channels.py`,
  `linkedin_scout/heuristics.py`, `hunter/sent_parse.py` — these detect Russian text
  in job postings and must stay Russian
- Russian **code comments** in `llm_client.py`, `hunter/delivery.py`, `hunter/best_effort.py`
  (docstring owner quotes) — cosmetic, not user-facing
- Russian **test fixture data** in `tests/test_doomed_gate.py`, `tests/test_filters_classify.py`,
  `tests/test_linkedin_scout.py`, `tests/test_lang_guard_cyrillic.py`,
  `tests/test_telegram_channels_source.py`, `tests/test_sent_parse.py`,
  `tests/test_sent_normalizer.py` — these test Russian-content detection and must stay
- Other files not listed above

## Verification

After making the changes:

1. `python -m compileall hunter/best_effort.py hunter/gmail_report.py`
2. `ruff check hunter/best_effort.py hunter/gmail_report.py tests/test_best_effort.py tests/test_gmail_report.py`
3. `ruff format hunter/best_effort.py hunter/gmail_report.py tests/test_best_effort.py tests/test_gmail_report.py`
4. `pytest tests/test_best_effort.py tests/test_gmail_report.py -v`
5. Confirm no remaining Cyrillic in either production file outside of comments:
   `grep -P "[а-яА-ЯёЁ]" hunter/best_effort.py hunter/gmail_report.py` — should
   return only the docstring comment lines (best_effort.py lines 9-10), nothing in
   gmail_report.py

## Commit

Single commit. Message style: imperative, lowercase after prefix.

```
Translate Russian Telegram messages to English in best_effort + gmail_report
```
