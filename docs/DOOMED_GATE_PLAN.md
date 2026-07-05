# Doomed-vacancy gate — отсев обречённых вакансий ДО генерации CV

**Branch:** `feat/doomed-vacancy-gate` (from `origin/master` @ 75c38ad, поверх
смерженного verdict-refine-loop PR #115)
**Status:** PLANNED — ready for implementation (реализация в ЭТОЙ ветке/PR,
один коммит на милстоун)
**Scope:** `hunter/filters.py`, wiring в `apply_api.py`/`apply_cli.py`,
`tools/screen_calibrate.py`, config, tests, CLAUDE.md

---

## Зачем (по-русски, для владельца)

Часть заявок обречена ещё до генерации CV — и никакой ATS-скор это не лечит:

- **BigbearAI** (2026-07-02): гибрид в McLean, Virginia. Вердикт 92%, CV
  сгенерирован, $0.19 потрачен — а рекрутер видит Wrocław в адресе и закрывает.
  Владелец сам пометил в трекере «гибрид в америке».
- Аудит строк 670–767 (июнь): ~45 нежелательных CV. Listing-уровень уже
  зачищен (PR #110: fullstack+backend, body-disqualifiers, onsite-city для
  польских/кипрских городов). НО: **полный текст вакансии** — который часто
  раскрывает гибрид/локацию, невидимые в листинге, — до сих пор проверяется
  только на manual-paste пути (`screen_job_text`, warn-but-allow) и никогда
  не блокирует.

Этот PR добавляет **вторую линию обороны на полном тексте**, между
`fetch_job_text` и LLM-генерацией: детерминированный гейт (regex, ноль LLM,
ноль долларов), который скипает заведомо мёртвые заявки и предупреждает о
сомнительных.

```
Сейчас:  fetch_job_text → expired-check → LLM → docs → verdict   (~$0.20)
Станет:  fetch_job_text → expired-check → [GATE] → LLM → ...
                                    HARD-провал → SKIP-строка в трекере
                                      + Telegram однострочник, $0.00
                                    SOFT-провал → генерим, но в Telegram
                                      предупреждение
```

## Ключевые решения (зафиксированы, не пересматривать)

1. **Только детерминированно.** Никаких LLM-вызовов в гейте — regex/эвристики
   в `hunter/filters.py`, в стиле существующих `_is_unwanted_onsite_location`
   / `screen_job_text`. Цена проверки — ноль.
2. **Два семейства правил, разная строгость:**
   - **HARD → скип генерации** (высокая точность, ложное срабатывание почти
     исключено):
     a) он-сайт/гибрид, привязанный к географии ВНЕ Польши: US-штаты и города
        (`\bon-?site\b|\bhybrid\b` в окне ~120 симв. от US-штата/города,
        `McLean|Virginia|VA\b|New York|Austin|...`), страны/города Западной
        Европы и UK/США/Канады и т.п. — ГЕНЕРАЛИЗАЦИЯ существующей
        anti-hybrid-city логики со списка польских городов на «не-Польша»;
     b) требование права на работу/гражданства не-ЕС: `W2|C2C|H1B|US citizen|
        green card|security clearance|must be (located|based) in the (US|UK|...)`;
     c) required язык, которым кандидат не владеет: немецкий уже фильтруется
        на listing-уровне — перенести/переиспользовать German-детектор и на
        полный текст (французский/голландский по аналогии, узкий список).
     Вето (как в существующем коде): явный fully-remote сигнал
     (`fully remote|100% remote|remote (from )?(anywhere|europe|poland)|praca
     w pełni zdalna`) или упоминание Wrocław / weekly-hybrid Warsaw/Kraków
     исключение — HARD-правило (a) НЕ срабатывает.
   - **SOFT → генерим + предупреждение в Telegram** (точность ниже, решает
     человек): primary-стек не кандидата — Vue/Svelte/Ember-first вакансия,
     где Angular/React отсутствуют в требованиях (кейс Megaport: Vue 3/Nuxt,
     вердикт-потолок 82). Аккуратно: «Angular or Vue» / «React or Vue» — НЕ
     срабатывает; сигнал только когда чужой фреймворк в требованиях есть, а
     Angular И React оба отсутствуют во всём тексте.
3. **Переиспользовать, не дублировать.** Новая функция
   `filters.assess_job_text(job_text, *, title="", company="") ->
   list[GateFinding]` (`GateFinding = dataclass(rule: str, severity:
   "hard"|"soft", evidence: str)` — evidence: короткая цитата-подстрока для
   Telegram/лога). Существующий `screen_job_text` внутри переключается на
   `assess_job_text` (его текущие проверки становятся частью soft/hard
   набора) — БЕЗ изменения его контракта warn-but-allow для paste-пути.
   Regex-списки — модульные константы (стиль filters.py), каждый с тестом.
4. **Точки врезки** (обе — ПОСЛЕ expired-check, ДО первого LLM-вызова):
   - `apply_api`: рядом с существующим manual-screen шагом (Step 1.5e);
   - `apply_cli`: симметрично;
   - Поведение: HARD и НЕ (force-режим или paste) → записать SKIP-строку
     через существующий `tracker.add_skipped(job)`-путь (посмотреть, как
     Skip-кнопка формирует Job/строку; причина — в Telegram-сообщении:
     `⛔ Skipped before generation: {rule} — "{evidence}" {url}`), выйти чисто
     (API `sys.exit(0)`-паттерн как у соседних гейтов, CLI — return).
     Force-режим и paste: HARD деградирует до warn (существующая семантика
     «владелец сказал надо — генерим», предупреждение остаётся).
     SOFT → продолжить, добавив warn-строку в существующее
     manual-screen-предупреждение / отдельным сообщением на AUTO-пути.
   - Hunt-loop (listing-уровень) НЕ трогать — он уже покрыт PR #110; гейт
     работает только там, где есть полный текст.
5. **Config** (`hunter/config.py` + `.env.example` + таблица CLAUDE.md):
   `DOOMED_GATE_ENABLED` (default `true`), `DOOMED_GATE_HARD_ACTION`
   (`skip` default / `warn` — аварийный рычаг, если точность на живых данных
   окажется хуже калибровки).

## Милстоуны

| M | Что | Файлы |
|---|---|---|
| M1 | `GateFinding` + `assess_job_text` + все regex-семейства; `screen_job_text` переведён на него; юнит-тесты на каждое правило И на каждое вето (fully-remote, Wrocław, Warsaw/Kraków-weekly, «Angular or Vue»). Фикстуры — РЕАЛЬНЫЕ тексты: скопировать `job_posting.txt` BigbearAI и Megaport из `Applications/2026-07-02/…` в `tests/fixtures/doomed_gate/`, + 2–3 «хороших» (Fairmarkit/Comarch/LuxMed 2026-07-03/04) как негативные кейсы | `hunter/filters.py`, `tests/test_doomed_gate.py`, fixtures |
| M2 | Врезка в оба пайплайна (решение №4): SKIP-строка, Telegram, force/paste-деградация; wiring-тесты (hard→skip+SKIP-строка; hard+force→warn+генерация; soft→warn+генерация; gate disabled→noop) | `hunter/apply_api.py`, `hunter/apply_cli.py`, тесты |
| M3 | Config + docs: `.env.example`, CLAUDE.md (таблица конфигов, Pipeline Flow шаг «1.5f Doomed gate», Agent Work Log) | config, docs |
| M4 | **КАЛИБРОВКА НА РЕАЛЬНЫХ ДАННЫХ — главное требование владельца, PR без неё не готов.** См. раздел ниже | `tools/screen_calibrate.py` |

## M4 — калибровка на реальных вакансиях из трекера

Требование владельца: «проверь на реальных линках из таблицы в гугл шитах».

Новый `tools/screen_calibrate.py` (read-only, dry-run по умолчанию), два
источника данных:

1. **Офлайн-корпус (основной, без сети):** все `Applications/**/job_posting.txt`
   (~363 файла на этой машине) — прогнать через `assess_job_text`. Компанию
   брать из имени папки. Это даёт масштаб и не зависит от протухших ссылок.
2. **Живые ссылки из Google Sheet (spot-check):** прочитать вкладку Tracker
   (паттерн — `tools/stats_sheet.py`: `gsheets_client.build_service` +
   `gsheets_state.json`, креды уже в корне репо), взять строки за последние
   ~45 дней с URL; для ~15–20 не-LinkedIn URL (LinkedIn без
   `LINKEDIN_STORAGE_STATE` локально даёт 429 — скипать по хосту) сделать
   `hunter.sources.fetch_job_text(url)` ПОСЛЕДОВАТЕЛЬНО с паузой 2–3 сек,
   толерантно к ошибкам (протухшие/Cloudflare — считать и пропускать),
   и прогнать гейт по живому тексту.

**Сверка с разметкой владельца.** Колонка Sent в Sheet — это его ground truth:
заметки «гибрид», «офис», «не подходит», названия городов → такие строки гейт
ДОЛЖЕН ловить (hard или soft); строки с реальной датой отправки (значит,
владелец счёл вакансию годной) → HARD-срабатывание на них = **false positive,
блокер**. Вывод скрипта — таблица `company | rule | severity | evidence |
owner_note` + сводка: сколько hard/soft/чисто, список всех hard-срабатываний
на отправленных строках.

**Критерий приёмки M4:**
- 0 (ноль) HARD-срабатываний на вакансиях, которые владелец реально отправил
  (Sent = дата) — каждое найденное чинить сужением regex, не оправдывать;
- BigbearAI (`гибрид в америке`) пойман HARD-правилом (a);
- Megaport пойман SOFT-правилом (stack-mismatch);
- полный отчёт калибровки — в описание PR (сокращённо) и целиком в
  `docs/DOOMED_GATE_CALIBRATION.md`.

## Explicitly OUT of scope

- Никаких LLM-вызовов в гейте (стоимость и латентность нулевые by design).
- Listing-уровень фильтров (PR #110) не трогать.
- Дедуп «повторок» (reposted-роли с новым URL) — известная отложенная тема,
  не сюда.
- `verdict_refine` / judge / язык-гейт — не трогать вообще.

## Definition of done

- [ ] `python -m pytest tests/ -q` зелёный, КРОМЕ 3 известных pre-existing
      падений на этой машине (test_cost_writer ×2 + test_verdict_writer ×1 —
      из-за локального tracker.xlsx; их не чинить, задача заведена отдельно).
- [ ] `python -m ruff check .` чистый; `python -m compileall .` ок.
- [ ] Калибровка M4 прогнана, критерии выполнены, отчёт в
      docs/DOOMED_GATE_CALIBRATION.md + краткая выжимка в PR.
- [ ] CLAUDE.md обновлён в том же PR (конфиг-таблица, Pipeline Flow, Work Log).
- [ ] Один коммит на милстоун, `git push -u origin feat/doomed-vacancy-gate`,
      PR на master через `gh pr create` (в конце тела:
      `🤖 Generated with [Claude Code](https://claude.com/claude-code)`).
