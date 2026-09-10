# Engineering Plan — зрелость кодовой базы и процесса разработки

**Status:** draft
**Date:** 2026-09-09
**Роль:** ведущий инженер / tech lead
**Motivation:** кодовая база в хорошей форме для соло-проекта (~25k строк прод,
~42k строк тестов, 206 тест-файлов, ruff-гейт, golden E2E, журнал решений), но
несколько привычек, оправданных для одного человека, станут тормозом для
команды из двух и для продукта с клиентами. Этот план про сам процесс и форму
кода, не про архитектуру (это `03-ARCHITECTURE_PLAN.md`) и не про фичи.

---

## Problem

Факты, проверенные по дереву 2026-09-09.

| # | Наблюдение | Доказательство | Почему это проблема |
|---|---|---|---|
| 1 | Документация как журнал, а не как карта | `CLAUDE.md` 2536 строк, каждая запись Repository Layout это абзац истории инцидентов; `docs/AGENT_LOG.md` отдельно | Новый разработчик (или агент) читает 2500 строк, чтобы найти, где конфиг. Контекстное окно агента тратится на историю вместо задачи |
| 2 | Конфиг это глобальное состояние на импорте | `hunter/config.py`: 105 обращений к env на уровне модуля; `.env.example` покрывает 60 из них (см. `06-OPS_PLAN.md`) | Тесты вынуждены monkeypatch-ить модуль; per-user настройки реализуются env-инъекцией в subprocess |
| 3 | Два пайплайна генерации | `hunter/apply_api.py` 1205 + `hunter/apply_cli.py` 1087 строк; CLAUDE.md документирует 4 инцидента дрейфа за 5 недель | Каждая стадия пишется дважды; golden CLI E2E появился только после четвёртого инцидента |
| 4 | Типизация не гейтится | mypy `continue-on-error`, baseline 218 ошибок | Ошибки типов копятся, никто не смотрит на informational job |
| 5 | Порога покрытия нет | `pytest --cov` без `--cov-fail-under`; `docs/quality/04` откладывает «на несколько недель» с июля | Слепые зоны не картированы |
| 6 | Прогон тестов 10 минут последовательно | README; `pyproject.toml` без `-n auto`/маркеров slow | Медленная петля обратной связи, соблазн не запускать локально |
| 7 | `tracker.py` 2203 строки, `generate_docs.py` 560 строк с python-docx-макетом внутри | wc | Один файл знает про очередь, дедуп, Sheets-метаданные, cost, verdict, erasure |
| 8 | Один образ на всё | `Dockerfile`: python + LibreOffice + Node + Claude CLI + Chromium, ~2 GB | Медленная сборка, широкая поверхность, нельзя масштабировать по частям |
| 9 | Скорость разработки агентами опережает понимание | 184 коммита в июле, 37 в августе; PR-ревью через CodeRabbit + агенты | Риск: код, который понимает только модель, написавшая его |
| 10 | Windows-дев против Linux-прод | LibreOffice путь `C:/Program Files/...` в `generate_docs.py`, `uv pip compile --python-platform linux` как ритуал | Локальная среда не равна прод; часть тестов терпима к отсутствию LibreOffice |

## Non-goals

- Переписывание на другой фреймворк/язык.
- Достижение 100% покрытия или 0 mypy-ошибок как самоцель.
- Снос `apply_shared.py`-шима до завершения `03-ARCHITECTURE_PLAN.md` M8.

---

## M0 — Measure ($0)

**M0.1 Карта покрытия по модулям.**
```bash
pytest tests/ --cov=hunter --cov=generate_docs --cov=llm_client --cov-report=term-missing -q | tail -80
```
Правило: модули с покрытием < 40% и > 200 строк попадают в список «слепые зоны»
M4; если таких > 8, M4 делится на два этапа.

**M0.2 Время тестов по файлам.**
```bash
pytest tests/ --durations=30 -q
```
Правило: если 10 самых медленных тестов дают > 40% времени, они получают
маркер `slow` и выносятся из pre-commit прогона (M3).

**M0.3 Кто читает CLAUDE.md.** Подсчёт: сколько строк CLAUDE.md ссылается на
инциденты/даты (`grep -c "2026-0"`), сколько на текущую структуру. Правило: если
> 50% строк это история, M1 (расщепление) первым.

**M0.4 mypy по модулям.**
```bash
mypy hunter/ llm_client.py generate_docs.py apply_agent.py 2>&1 | cut -d: -f1 | sort | uniq -c | sort -rn | head -20
```
Правило: файлы с 0 ошибок переходят в strict-список немедленно (M2 ratchet).

---

## M1..Mn — Milestones

### M1 — Документация: карта отдельно от журнала (1–2 коммита, 0 кода)

Целевая структура:

```
CLAUDE.md                 ≤ 300 строк: что это, как запустить, инварианты (правила), где что лежит (ссылки)
docs/ARCHITECTURE.md      обновить: топология, доменная модель (из 03-ARCHITECTURE M1), пайплайн по стадиям
docs/MODULES.md           таблица модуль → назначение → владелец-план (одна строка на модуль)
docs/INVARIANTS.md        правила, которые проверяет project-invariants-review (из CLAUDE.md «Important Rules»)
docs/AGENT_LOG.md         вся история инцидентов (уже есть) + перенос абзацев из Repository Layout
docs/CONFIG.md            таблица env/generation.yaml (сейчас внутри CLAUDE.md)
```

Правило переноса: строка остаётся в CLAUDE.md, только если она нужна, чтобы
**не сломать** код сегодня (инвариант); всё «почему так вышло» уходит в
AGENT_LOG с датой. Агенты `project-invariants-review`, `.coderabbit.yaml` и
`.claude/commands/*` обновляют ссылки. Проверка: `tests/test_handoff_readiness.py`
продолжает проходить; новый тест, что каждый файл из `hunter/` упомянут в
`docs/MODULES.md`.

### M2 — mypy ratchet (1 коммит + процесс)

`scripts/mypy_ratchet.py`: хранит baseline по файлам (`mypy_baseline.json`),
CI падает, если у файла ошибок стало больше или появился новый файл с
ошибками. Файлы с нулём ошибок попадают в `[tool.mypy] strict` список.
Раз в спринт: минус 20 ошибок. Проверка: намеренно добавить ошибку типа в
чистый файл, CI красный. Откат: снова `continue-on-error`.

### M3 — Быстрый локальный прогон

- `pytest-xdist` в `dev`-extra, `-n auto` в CI и в `pr.md` pre-flight.
- Маркер `slow` для subprocess/LibreOffice/Playwright-тестов; `pytest -m "not slow"`
  как pre-commit (цель < 90 секунд), полный прогон в CI.
- `pytest --durations=20` в CI job summary.
Проверка: время `not slow` прогона в README. Откат: маркеры безвредны.

### M4 — Порог покрытия по слепым зонам

По результату M0.1: для каждого модуля из списка пишутся тесты до 60%, затем
включается `--cov-fail-under=<текущее общее − 1>` как ratchet (не абсолютный
порог). Приоритет: `hunter/tracker.py` (erasure, scoping — пересекается с
`05-SECURITY_PLAN.md` M2), `hunter/gsheets_sync.py`, `hunter/gdrive_sync.py`,
`generate_docs.py`.

### M5 — `Settings` и конфиг (совместно с `03-ARCHITECTURE_PLAN.md` M2)

Инженерная часть: тест «каждое имя из `hunter/settings.py` есть в
`.env.example` и в `docs/CONFIG.md`» (расширение `test_handoff_readiness`
проверки (b)); удаление прямых `os.getenv` вне `settings.py` (ruff-правило
через `flake8-tidy-imports` banned-api или собственный тест-grep).

### M6 — Расщепление `tracker.py` по ролям строки (3–4 коммита, чистые перемещения)

Прецедент: `apply_shared.py` → `hunter/pipeline/*` (wave 1, три PR без
изменения поведения, шим сохраняет импорты). Целевые модули:

```
hunter/tracker/
  __init__.py     шим: re-export всего публичного (как apply_shared.py)
  dedup.py        is_known, dedup_key, normalize_url, get_known_company_titles
  queue.py        add_pending, claim_pending, release_claim, reset_stale_claims, count_*
  writes.py       add_applied/add_failed/add_skipped/add_expired/convert_*, _is_known_terminal
  stamps.py       set_cost, set_ats_verdict, set_to_learn, set_drive_url
  sheets_meta.py  sheets_row/sheets_dirty, mark_orphans_expired, insert_pulled_rows
  reads.py        lookup_url, get_failed_jobs, iter_unsent_rows, read_all_tracker_rows
```

Каждый перенос сопровождается тестом на шим (`tests/test_tracker_shim.py`,
как `test_apply_shared_shim.py`). Это же место, где `05-SECURITY_PLAN.md` M2
добавляет `user_id` во все запросы: делать одновременно, файл за файлом.

### M7 — `generate_docs.py`: макет отдельно от оркестрации

`hunter/render/layout.py` (python-docx макет: `build_resume`,
`build_cover_letter`, стили из `document.*` generation.yaml),
`hunter/render/pdf.py` (LibreOffice вызов, путь из настроек, поиск бинаря по
платформе), `generate_docs.py` остаётся CLI-обёрткой с трекер-записью.
Проверка: байтовое сравнение DOCX на 3 фикстурах до/после (XML внутри zip,
без timestamps). Подготавливает рендер-воркер из `06-OPS_PLAN.md` M8.

### M8 — Образы по ролям

`Dockerfile.bot` (python + PTB, без LibreOffice/Chromium), `Dockerfile.worker`
(python + LibreOffice + CLI), `Dockerfile.scraper` (python + Chromium) на общей
базе `Dockerfile.base`. Multi-stage, `USER app` (см. `05-SECURITY_PLAN.md`
M1). Проверка: размер каждого образа в CI summary; бот < 400 MB. Делается
после `03-ARCHITECTURE_PLAN.md` M4 (пока apply исполняется внутри бота, образ
один).

### M9 — Дев-среда = прод

`docker-compose.dev.yml` с теми же образами + `make test-docker`; LibreOffice
путь только из настроек, тесты рендера пропускаются с явной причиной, если
бинаря нет (уже частично так). Скрипт `scripts/bootstrap_dev.ps1`/`.sh`:
клонирование трёх репо, `git config core.hooksPath .githooks`, копирование
`.example`-файлов.

### M10 — Процесс для двоих

- **ADR** вместо абзацев в CLAUDE.md: `docs/adr/NNNN-*.md` (контекст, решение,
  последствия, дата); `plan-doc` скилл дополняется шагом «после shipped →
  ADR».
- **Definition of Done** в `pr.md`: тест, CLAUDE.md/MODULES.md, ADR при
  архитектурном решении, английский коммит, без атрибуции.
- **Код, который читает человек:** правило для агентских PR: объяснение
  «почему» в PR-описании ≤ 10 строк, остальное в ADR; docstring модуля ≤ 15
  строк (история в AGENT_LOG).
- **Еженедельный 30-минутный обзор** метрик из `08-DATA_EVAL_PLAN.md` M1 и
  `/fails`, чтобы понимание системы не отставало от изменений.

---

## Risks

| Риск | Чем ловится |
|---|---|
| M1 потеряет инвариант при переносе строк | `project-invariants-review` и `.coderabbit.yaml` обновляются в том же PR; тест на упоминание модулей |
| M6/M7 «чистые перемещения» изменят поведение | Тесты шима + golden E2E + байтовое сравнение DOCX |
| Ratchet-и раздражают и их отключают | Ratchet допускает не рост, а не ноль; порог движется только вниз |
| Расщепление образов сломает деплой | Делать после появления сервиса, старый образ остаётся на релиз |

## Cost

Ноль LLM-вызовов. Время: M1 два дня, M2–M3 день, M4 неделя, M6–M7 по неделе.

## Open questions

1. CLAUDE.md ≤ 300 строк с переносом истории в AGENT_LOG (да/нет)?
2. Ratchet-гейты (mypy, coverage) как блокирующие в CI (да/нет)?
3. Расщепление `tracker.py` совместить с добавлением `user_id` во все запросы (да/нет)?
4. ADR как формат решений вместо абзацев в CLAUDE.md (да/нет)?
