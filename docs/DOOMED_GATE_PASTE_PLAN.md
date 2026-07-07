# Doomed-gate paste-path extension — план

**Branch:** `fix/doomed-gate-paste-path` (from `origin/master` @ 5b9e3ec, поверх
смерженного PR #116 doomed-vacancy-gate + PR #117 verdict refine)
**Status:** DONE — see `docs/DOOMED_GATE_PASTE_CALIBRATION.md` for the result
**Scope:** `hunter/filters.py`, `hunter/apply_shared.py`, `hunter/apply_api.py`,
`hunter/apply_cli.py`, `hunter/services/apply_service.py`, tests

## Зачем

Разбор строк трекера 2026-07-02…07-06 (до этих правок) показал, что часть
"должны были отфильтровать" вакансий пришла через **ручную вставку ссылки**
(paste), которая по дизайну (docstring `screen_job_text`) сознательно
пропускает title-фильтры — предполагается, что раз владелец вставил ссылку
сам, он знает, что делает. Три реальных случая это опровергают:

- **Santander** `.NET Developer (Angular)`, ATS 72%, note "должны были
  фильровать" — тайтл не содержит слова "fullstack", поэтому
  `_is_unwanted_fullstack` (которая требует буквально "fullstack" в тайтле)
  не срабатывает; ".NET" в тайтле поймал бы `_matches_exclude_pattern`
  на listing-уровне, но эта проверка не переиспользуется в
  `assess_job_text` вообще.
- **QuantumBlackMcKinsey** `Software Engineer - QuantumBlack, AI by
  McKinsey`, ATS 82%, note "фулстек, должны были отфильтровать" — тайтл не
  проходит `_matches_title_keywords` (не фронтенд-тайтл), но это тоже не
  проверяется в гейте.
- **Comarch ×3**, ATS 96%, note "гибрид не вроцлав" — тело вакансии вообще
  не содержит слов hybrid/onsite, только "Comarch Warsaw, Mazowieckie,
  Poland" в шапке (LinkedIn). Существующий `_assess_foreign_onsite` /
  `_is_unwanted_onsite_location` требуют onsite-сигнал РЯДОМ с городом —
  здесь его просто нет в тексте.

Все три случая по $0.15–0.20 впустую потраченных на LLM, при финальном ATS
72–96% (два откровенно мусорных, один формально "хороший" скор при плохой
локации).

## Решения

1. **Force ≠ paste.** Сейчас `is_manual_override = bool(paste_text) or
   skip_dedup` в обоих пайплайнах — HARD-находка гейта деградирует до warn
   одинаково что для `/force`, что для простой вставки ссылки. Разделяем:
   `/force` (`skip_dedup`) — явная команда владельца "сгенерируй именно
   это", остаётся полным оверрайдом (HARD→warn). Обычный paste — HARD
   теперь **блокирует** так же, как авто-путь (никакой автоматической
   деградации только по факту paste). Параметр переименован
   `is_manual_override` → `is_force_override`.
2. **Guessed title для paste-пути.** Новый `_guess_title_from_text(job_text)`
   — эвристика "первая осмысленная строка" (пропускает пустые строки, nav-
   мусор вроде "Skip to main content"/"Sign in"/"Home", слишком короткие/
   длинные строки). Используется ТОЛЬКО когда явный `title` не передан
   (paste), никогда не переопределяет уже известный title (hunt/JobLeads).
   Best-effort — промах просто ничего не ловит, ложных срабатываний не
   создаёт (см. п.3, куда именно она идёт).
3. **Новые правила в `assess_job_text`** с учётом guessed title:
   - **HARD** `title_exclude_pattern` — переиспользует
     `_matches_exclude_pattern(effective_title)` (тот же список: .NET/Java/
     C#/PHP/Vue/Magento/…). Ловит Santander.
   - **SOFT** `off_domain_title` — переиспользует
     `not _matches_title_keywords(effective_title)`. SOFT, не HARD: угаданный
     тайтл менее надёжен, чем реальный (риск ложного HARD-блока на хорошей
     вакансии слишком высок для guessed title) — так что ловит QuantumBlack,
     но только предупреждением.
   - **Comarch НЕ ловится.** Первая версия плана включала SOFT-правило
     `header_location_anti_hybrid_city` (anti-hybrid город в первых ~500
     символах текста, без явного onsite/hybrid слова рядом). Реализовано и
     тут же отклонено при повторном прогоне `tools/screen_calibrate.py`:
     оно сработало и на **Fairmarkit** — реальной, полностью описанной,
     отправленной (98% ATS) вакансии с офисом в Варшаве без единого
     hybrid/onsite слова, ничем не отличимой в тексте от Comarch. Раз даже
     SOFT-версия не может отличить "просто HQ в Варшаве" от "гибрид,
     проверено на практике", — правило удалено, а не отгружено как шум на
     хороших вакансиях. Вывод: Comarch-кейс (owner note "гибрид не
     вроцлав") в принципе не решается по тексту без выдумывания —
     информация просто отсутствует в fetched job_text.
4. **Найден и починен смежный баг: title никогда не доходил до гейта для
   НЕ-JobLeads заявок.** `--company`/`--title` в `apply_service.py` передавались
   в subprocess ТОЛЬКО для `jobleads.com` URL — то есть для любого обычного
   auto-hunt приложения `run_doomed_gate` всегда видел `title=""`, и
   ДВЕ УЖЕ СУЩЕСТВОВАВШИЕ (до этого PR) title-зависимые проверки —
   `_is_unwanted_fullstack` и переиспользованный в новом `title_exclude_pattern`
   `_matches_exclude_pattern` — были мертвым кодом внутри гейта для всех
   источников кроме JobLeads. Исправлено: `--company`/`--title` теперь
   передаются для любой auto-hunt заявки с известным тайтлом (не только
   paste — так гейт становится точным без угадывания везде, где тайтл вообще
   есть). Владелец подтвердил (см. обсуждение в чате) оставить это как есть,
   несмотря на то что это задним числом ужесточает поведение для всех
   auto-hunt заявок, а не только paste.
5. **Перекалибровка** (`tools/screen_calibrate.py --live`, оффлайн-корпус +
   live-выборка, с реальными тайтлами из Sheet вместо угадывания — см.
   `docs/DOOMED_GATE_PASTE_CALIBRATION.md`): 0 HARD false positives. Починка
   п.4 заодно ВПЕРВЫЕ активировала `is_unwanted_fullstack`/
   `title_exclude_pattern` на 3 старых Sent-строках (Unide ×2, BCFSoftware) —
   это не regex-неточность, а уже существующая, уже одобренная владельцем
   политика, которая раньше молчала из-за бага; задокументировано в
   `_PRE_EXISTING_POLICY_RULES`, не "починено" ослаблением паттерна.

## Explicitly OUT of scope

- Не меняем `/force` семантику — она остаётся полным оверрайдом.
- Не пытаемся парсить точную геолокацию (это эвристика на уровне SOFT, не
  замена `job.location`).
- `screen_job_text` (Step 1.5e) технически теперь ТОЖЕ показывает
  `off_domain_title` (т.к. переиспользует `assess_job_text`) — это осознанное
  побочное расширение (см. обновлённый docstring), а не отдельная задача.

## Definition of done

- [x] `python -m pytest tests/ -q` зелёный (1721 passed на этой машине).
- [x] `python -m ruff check .` чистый; `python -m compileall .` ок.
- [x] `python tools/screen_calibrate.py --live` — 0 HARD false positives
      (см. `docs/DOOMED_GATE_PASTE_CALIBRATION.md`).
- [x] Santander/QuantumBlack подтверждены как caught (юнит-тесты с реальными
      тайтлами + guessed-title вариант); Comarch — сознательно НЕ ловится
      (см. решение №3 выше, Fairmarkit false positive).
- [x] CLAUDE.md обновлён (Agent Work Log; config/pipeline flow структурно не
      меняются — доп. правила являются частью существующего Step 1.5f).
