# 09 — Мульти-трек: React как второй активный стек

**Приоритет:** P2 сейчас → **P0 в момент, когда владелец доучит React**
**Усилие:** ~1 день (инфраструктура уже наполовину существует)
**Ветка:** `feat/candidate-tracks`

## Для чего апдейт

Владелец доучивает React и скоро начнёт подаваться и на React-роли. Сегодня
система **активно отбрасывает** React-only вакансии на трёх уровнях, при том
что вся генерационная инфраструктура для React уже готова:

**Уже готово (менять не надо):**
- `prompts/base_cv_react.md` — React/JS-трек CV существует;
- `hunter/apply_api.py::_detect_stack_hint` уже мапит react/javascript →
  `base_cv_react.md`;
- источники **уже ищут** по запросу "react" (justjoin/nofluff/findmyremote/
  thesmartjobs/… — тройка запросов angular/frontend/react) — кандидаты
  приходят, но выбрасываются фильтром;
- doomed gate уже считает React «своим» стеком (SOFT stack-mismatch срабатывает
  только когда «neither Angular nor React present»).

**Что отбрасывает React сейчас (три уровня запрета):**
1. Listing-уровень: `hunter/filters.py::_is_react_only_title` +
   `_is_react_without_angular` — React-без-Angular вакансия убивается ещё в
   hunt-цикле;
2. Apply Step 1.5c: pre-LLM текстовый React-only чек (экономил LLM-вызов);
3. Apply Step 4.5: скип React-only после генерации (страховка), обходится
   только `--force`.

## Как именно будет происходить

### Конфиг треков

`CANDIDATE_TRACKS` (env, default `angular`; после доучивания —
`angular,react`), парсится в `hunter/config.py` в `TRACKS: frozenset[str]`.
Если к этому моменту сделан док 08 — поле живёт в `candidate.yaml`
(`tracks.enabled`), env остаётся override'ом. Плюс runtime-переключатель без
рестарта — DB-ключ `tracks_enabled` + команда `/tracks [angular|react|both]`
по образцу `/dual` (паттерн DB-key-wins-over-env уже отработан на
`dual_shadow_profile`).

### Изменение фильтров — «запреты становятся условными»

Все три уровня получают один и тот же гейт:

```python
def _react_track_active() -> bool:
    return "react" in active_tracks()
```

1. `filters.py`: `_is_react_only_title` / `_is_react_without_angular`
   возвращают False (не фильтруем), когда react-трек активен. Сама функция
   НЕ удаляется — она продолжает работать как **классификатор** для выбора
   base CV и для статистики.
2. Apply Step 1.5c и Step 4.5: те же условия. `--force`-обход остаётся как был.
3. Doomed gate: изменений не требует (React уже «свой»). Проверить только
   тексты SOFT-warning'ов, чтобы не писали «not the candidate's stack» на
   React-роль при активном треке.

### Сопутствующее (в тот же PR)

- `_detect_stack_hint`: сегодня она вызывается на уже отфильтрованном потоке;
  убедиться тестами, что чистый React-постинг → `base_cv_react.md`, а
  fullstack React+Node → `base_cv_fullstack_react_next.md`.
- `/status` показывает активные треки (одна строка).
- Funnel-аналитика: `/funnel` уже считает per-source; per-track разрез НЕ
  строим заранее (правило против спекулятивных слоёв) — стек и так пишется в
  колонку Stack трекера, разрез можно снять запросом когда понадобится.
- `MAX_JOBS_PER_RUN`/расписание не трогаем: рост потока вакансий после
  включения react оценить по факту первой недели (`/health`), лимит — ручка
  в .env на этот случай.

### Будущие треки — бесплатно

Схема расширяема без нового кода: трек = запись в конфиге + base_cv файл +
(опционально) свой фильтр-запрет, который становится условным. AI-трек
(`base_cv_ai.md`) уже существует как base CV — его «включение» как трека
потребует только конфиг-строки, фильтров-запретов на него нет.

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `hunter/config.py` | `CANDIDATE_TRACKS` env → `TRACKS`; helper `active_tracks()` (env + DB-ключ) |
| `hunter/filters.py` | Гейт `_react_track_active()` в `_is_react_only_title`-потребителях (`classify_job`) |
| `hunter/apply_api.py` / `hunter/apply_cli.py` (или `pipeline/stages` после дока 05) | Условие трека в Step 1.5c и Step 4.5 |
| `hunter/commands/tracks.py` | **Новый**: `/tracks` show/switch (по образцу `commands/dual.py`) |
| `hunter/commands/status.py` | Строка «Tracks: angular (+react)» |
| `tests/test_tracks.py` | **Новый**: параметризация обоих режимов — react-only вакансия: отфильтрована при `angular`, прошла до base_cv_react при `angular,react`; angular-поведение бит-в-бит неизменно |
| `CLAUDE.md` | Key Configuration (+`CANDIDATE_TRACKS`), список команд (+`/tracks`) |

## Критерий готовности

- При `CANDIDATE_TRACKS=angular` (default) полный suite зелёный **без правки
  ни одного существующего ассерта** — поведение сегодняшнего дня не изменилось.
- При `angular,react`: реальная React-only вакансия из фикстур проходит hunt →
  apply → CV генерируется из `base_cv_react.md` (golden-сценарий в E2E из
  дока 04).
- `/tracks react on` меняет поведение без рестарта бота.

## Риски

- Включение react заметно увеличит поток (react-вакансий на рынке больше,
  чем angular) → расход LLM вырастет. Митигация: включать сначала в
  MANUAL-режиме на неделю (посмотреть качество потока карточками), потом
  AUTO; `MAX_JOBS_PER_RUN` как предохранитель.
- Качество react-CV: `base_cv_react.md` до сих пор использовался только для
  fullstack/JS-ролей через `--force` — перед включением трека прогнать
  `tools/preview_apply.py` на react-фикстурах из `tests/fixtures/sample_jobs/`
  и отсмотреть 2–3 CV глазами.
