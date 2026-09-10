# Architecture Plan — от процесса бота к платформе

**Status:** draft
**Date:** 2026-09-09
**Роль:** архитектор
**Motivation:** `docs/SAAS_PIVOT_PLAN_supersedes_PYTHON_CORE_PLAN.md` задаёт верную
целевую схему (Python-сервис как единственный исполнитель, outbox, shared secret,
`user_id` только от NestJS). Этот план не переписывает её, а (а) уточняет порядок
трёх решений, где план пивота, по моему мнению, ошибается в последовательности,
(б) вводит явную доменную модель вместо одной таблицы с 15 статусами, (в) описывает
границы модулей так, чтобы каждую следующую фичу можно было положить в
определённое место.

Все факты проверены по рабочему дереву 2026-09-09; ссылки на файлы даны для
перепроверки.

---

## Problem

Текущая топология (факт):

```
                 ┌──────────────── VPS, один хост ────────────────┐
Cloudflare Tunnel│  site (nginx)   api (NestJS)   bot (PTB)       │
                 │        │           │   │          │   │        │
                 │        └── /api ───┘   │          │   │        │
                 │                 app.sqlite    tracker.db ◄─────┤ два писателя
                 │                        └──── users/{uid}/ ────┘ файловая система
                 │                                   │
                 │                        apply_agent.py (subprocess, env-инъекция)
                 │                                   │
                 │                        LibreOffice · Playwright · cloudscraper
                 └────────────────────────────────────────────────┘
```

Пять структурных проблем, каждая с доказательством:

1. **Исполнение apply живёт внутри процесса Telegram.** `hunter/apply_worker.py`
   запускается из `telegram_bot._post_init` через `app.create_task`;
   `hunter/schedules/__init__.py::register(app, tz)` принимает PTB `Application`,
   каждый callback берёт `ContextTypes`. Рестарт бота ради правки команды убивает
   генерацию клиента (частично спасает `reset_stale_claims`, но через 60 минут).
2. **Два процесса пишут один SQLite-файл через Docker-том.** Бот (`hunter/db.py`) и
   NestJS (`api/src/db/migrations.ts`, `tracker-migrations.ts`) оба владеют
   «идемпотентно зеркалируемой» DDL. Уже есть таблицы, которые «API пишет, бот
   дренирует» (`profile_jobs`, `telegram_link_codes`). Два владельца схемы это
   дрейф по определению; план пивота §3.3 это признаёт, но откладывает Postgres
   «до первого клиента» (Stage 8).
3. **Тенантность реализована env-инъекцией в subprocess.** `hunter/users.py::user_env`
   подставляет `JOB_HUNTER_USER_ID`, `CANDIDATE_YAML_PATH`, `APPLICATIONS_DIR`;
   `hunter/config.py` читает 105 env-обращений на уровне модуля при импорте. Это
   работает, потому что apply это отдельный процесс на вакансию, но делает
   невозможным in-process исполнение для нескольких пользователей и делает конфиг
   глобальным состоянием, которое тесты вынуждены monkeypatch'ить.
4. **Два пайплайна генерации.** `hunter/apply_api.py` (1205 строк) и
   `hunter/apply_cli.py` (1087 строк) зеркалят стадии вручную; CLAUDE.md
   документирует четыре инцидента за пять недель из-за дрейфа CLI-ветки. CLI-ветка
   работает через личную подписку Claude, что для клиентов неприменимо (см.
   `07-COMPLIANCE_PLAN.md`).
5. **Домен спрятан в одной таблице.** `applications` хранит и вакансию, и
   заявку, и очередь (`PENDING`/`IN_PROGRESS` в `ats_status`), и документы (путь
   `folder`), и метрики (`cost_usd`, `ats_verdict`), и синхронизацию (`sheets_row`,
   `sheets_dirty`), и исход (`confirmation`, `answer`). `hunter/tracker.py` на 2203
   строки это следствие: каждая функция знает про все роли строки сразу.

## Non-goals

- Микросервисы. Два процесса Python (bot, service) плюс NestJS и nginx это
  потолок на ближайший год.
- Переписывание пайплайна генерации. Стадии (`hunter/pipeline/*`) остаются;
  меняется только то, кто их вызывает и откуда берёт контекст пользователя.
- Смена языка/фреймворка бэкенда. NestJS остаётся владельцем auth/users/billing.
- Отказ от Telegram. Он остаётся равноправной поверхностью (решение пивота §3.1).

---

## Целевая доменная модель

Вводится явно, до кода, чтобы каждая таблица и каждый модуль имели одного
владельца.

```
Account (NestJS)          users, auth, settings, credits ledger
  └── Profile (NestJS write / service read)   структурированный профиль + ревизии
        └── ProfileRender                     candidate.yaml / base_cv_* как кэш
Vacancy (service)         url_norm, source, posting_text, fetched_at, lang, expired_at
Tailoring (service)       account_id, vacancy_id, profile_revision, status, track,
                          prescore, verdict, cost_usd, refine_rounds, started/finished
  ├── Document            kind (cv_en/cv_pl/cl_en/…), storage_key, sha256, rendered_at
  ├── QualityReport       judge_report, lang_gate, scrubs, ats_check
  └── Outcome             sent_at, response, interview, reported_by (user/gmail)
Job (service)             kind (tailor/parse/render/preview), payload, status, claims
Outbox (service)          recipient, channel, payload_ref, attempts, state
Event (service)           account_id, type, ts, props   (продуктовая аналитика)
MarketSnapshot (service)  role, region, period, aggregates  (Stage 6 пивота)
```

Текущая `applications` это денормализованное объединение Vacancy + Tailoring +
Document + Outcome + Sheets-метаданных. Разрезать её сразу нельзя (2203 строки
tracker.py плюс NestJS читает её напрямую); поэтому ниже она сохраняется как
**read-model владельца** и постепенно перестаёт быть источником правды.

---

## Три поправки к плану пивота

### П1. Postgres на Stage 2, а не Stage 8

Аргумент плана: «пока нет клиентов, миграция ничего не даёт, кроме риска».
Контраргумент: Stage 2 создаёт **третьего писателя** (сервис) в тот же
SQLite-файл через общий том, а Stage 3 (outbox) и Stage 4 (профили) добавляют
таблицы, которыми снова будут владеть два репозитория. Делать это на SQLite и
затем переносить все три писателя на Postgres это две миграции вместо одной.
Кроме того, `tracker.py` без ORM переписывать придётся в любом случае; лучше
один раз, в момент, когда сервис и так получает новый слой доступа к данным.

Компромисс, который сохраняет безопасность рабочего бота: **бот остаётся на
SQLite до конца Stage 2**, сервис с первого дня на Postgres, единственная
таблица-мост это `Job` (очередь). Бот на Stage 2 не читает Postgres, он кладёт
задание через HTTP. `applications` в SQLite продолжает жить как read-model
владельца до Stage 5, после чего заполняется из Postgres проекцией.

### П2. API-only для любого `user_id`, кроме владельца

`llm_client.call_llm` при `LLMOutageError` делает `claude -p` через личную
подписку (CLAUDE.md, «CLI outage fallback»). Для клиента это (а) коммерческое
использование потребительской подписки, (б) второй пайплайн, который дрейфует.
Правило архитектуры: CLI-путь разрешён только при `current_user_id() ==
DEFAULT_USER_ID`; для остальных `LLMOutageError` превращается в `Job.status =
deferred` с повтором по расписанию и уведомлением через outbox. Точка гейта:
`llm_client.py` в месте выбора фоллбэка плюс `hunter/apply_agent.py::main()`
(пайплайн-уровневый retry через `main_cli`).

### П3. Единый владелец схемы: миграции только в сервисе

С момента появления сервиса **вся** DDL Postgres живёт в одном месте (Alembic
в репо бота, потому что 90% таблиц принадлежат сервису). NestJS получает
read-доступ к нужным таблицам и пишет только в свои (`users`, `credits`,
`profiles`), которые тоже мигрируются Alembic-ом по контракту. «Идемпотентное
зеркалирование DDL в двух репо» прекращается. Это ломает текущий принцип
«API owns `profile_jobs` schema»; менять его надо один раз и явно.

---

## Границы модулей (целевые)

```
hunter/
  core/         чистая генерация: (posting_text | None) + Profile + track → content.json + docs + scores
                НИКАКИХ: tracker, Telegram, Drive, Sheets, env, subprocess
  pipeline/     стадии как сейчас (gates, ats, scrubs, lang, abort) — вызываются из core
  scenarios/    owner_apply (dedup, tracker row, Drive, Sheets, outreach)
                customer_tailor (credits check, Tailoring row, outbox)
                standalone_optimize (Stage 6)
  service/      FastAPI: POST /jobs, GET /jobs/{id}, /health; auth shared-secret; user_id из заголовка доверенного вызывающего
  workers/      job runner (claim → scenario → finish), outbox drainer, nightly market aggregates
  storage/      DocumentStore (fs сейчас, R2 потом), одна абстракция put/get/delete/list
  sources/      как сейчас
  bot/, commands/, schedules/   Telegram — тонкий клиент сервиса
```

Ключевое правило: `core` принимает `Profile` (объект `hunter/profile_schema.py`),
а не путь к `candidate.yaml`. Сегодня `candidate.get()` читает файл через
`lru_cache`; в `core` это становится параметром. Рендер `candidate.yaml`
(`hunter/profile_render.py`) остаётся как кэш для существующих потребителей
(`gen_prompt.py`, фильтры), но источник правды это Profile из БД.

---

## M0 — Measure (0 кода)

Три чтения, каждое с правилом.

**M0.1 Карта зависимостей от глобального состояния.**
```bash
grep -rn "from hunter.config import\|hunter.config\." hunter/ --include=*.py | grep -v "^hunter/config.py" | wc -l
grep -rn "candidate.get(\|candidate.load(" hunter/ --include=*.py | wc -l
grep -rn "ContextTypes\|Application" hunter/main.py hunter/apply_worker.py hunter/schedules/ | wc -l
```
Правило: число мест, где `core`-кандидаты (`apply_api`, `pipeline/*`,
`verdict_refine`, `claim_judge`, `generate_docs`) читают `hunter.config`
напрямую, определяет объём M2. Если > 60, M2 делится на два коммита
(сначала `Settings`-объект с теми же значениями, потом инъекция).

**M0.2 Кто читает `applications` в NestJS.**
```bash
grep -rn "applications" D:/Projects/job-hunter/api/src --include=*.ts -l
```
Правило: если читателей > 5 файлов, `applications` остаётся read-model до
Stage 5 (как предложено выше); если ≤ 5, проекцию можно заменить view уже на
Stage 2.

**M0.3 Замер длительности стадий одной генерации** (для решения о
рендер-воркере): по `logs/` и `apply_stdout_log` разбить последние 20 прогонов
на fetch / LLM / judge / render / verdict / refine. Правило: если render
(LibreOffice) > 15% wall-clock, рендер выделяется в отдельный воркер на M6;
иначе остаётся внутри job runner.

---

## M1..Mn — Milestones

### M1 — Доменная модель как документ и контракт (1 коммит, 0 кода)

`docs/DOMAIN_MODEL.md`: сущности выше, владелец каждой, DDL Postgres v1,
маппинг «поле `applications` → сущность». Согласуется с репо api (его
`docs/RESUME_PROFILE_STORE.md` уже задаёт Profile). Проверка: каждая колонка
`applications` из `hunter/db.py` отображена ровно в одну сущность.

### M2 — `Settings`-объект вместо модульных констант (2 коммита)

`hunter/settings.py` на pydantic-settings: одна модель, те же имена, те же
дефолты, валидация типов. Коммит 1: `hunter/config.py` становится фасадом,
экспортирующим те же константы из `Settings()`; ни один потребитель не
меняется; тест `tests/test_config_facade.py` сверяет 100% имён. Коммит 2:
`core`-кандидаты получают `settings: Settings` параметром. Откат: фасад
остаётся, коммит 2 обратим по файлам.

Что это даёт мультитенантности: per-user переопределения (`user_settings`)
накладываются на копию объекта, а не через env-инъекцию.

### M3 — `core.tailor()` (Stage 1 пивота, уточнённый)

Сигнатура:
```python
def tailor(profile: Profile, posting: Posting | None, *, track: str,
           settings: Settings, llm: LLMClient, out_dir: Path) -> TailorResult
```
`TailorResult` = content.json + список документов + QualityReport + scores.
Внутри: существующие стадии `pipeline/*` в текущем порядке. Снаружи: ни
tracker, ни notify, ни Drive. `apply_api.main_api` становится сценарием
`scenarios/owner_apply.py`, который вызывает `core.tailor()` и делает всё
остальное. Проверка: golden E2E (`tests/test_golden_apply_e2e.py`) проходит
без изменений фикстур; байтовое сравнение content.json до/после на 3
фикстурах. Откат: `main_api` сохраняется до конца M4 как второй путь за флагом.

### M4 — Сервис + Job + Postgres (Stage 2 пивота с поправкой П1)

- Контейнер `service` (FastAPI, uvicorn, без публичного порта), Alembic,
  Postgres-контейнер с томом.
- Таблицы v1: `jobs`, `tailorings`, `documents`, `outbox`, `events`.
- Эндпоинты: `POST /v1/jobs` (kind, account_id из заголовка доверенного
  вызывающего, payload), `GET /v1/jobs/{id}`, `GET /healthz`.
- Воркер: claim через `UPDATE … RETURNING` (тот же примитив, что в
  `tracker.claim_pending`), сценарий по kind, finish/fail.
- Бот: `apply_worker_loop` перестаёт исполнять и начинает делегировать; очередь
  `PENDING` в SQLite остаётся только как входящий буфер владельца, дренируемый
  в `POST /v1/jobs`.
Проверка: неделя dogfooding владельца (условие плана пивота Stage 2), метрика
«0 потерянных заданий при рестарте бота» из event log. Откат: флаг
`APPLY_EXECUTOR=inline|service`.

### M5 — Outbox и уведомления (Stage 3 пивота, без изменений)

Сервис пишет `outbox`, дрейнер шлёт в email (api `src/mail`) и Telegram (через
бот, который владеет PTB). Проверка: убить бот, завершить job, поднять бот,
сообщение доставлено.

### M6 — DocumentStore и рендер-воркер (по результату M0.3)

`storage/DocumentStore` с fs-реализацией сейчас и R2 потом; все пути
`Applications/` идут через него. Если M0.3 показал рендер > 15%, LibreOffice
выделяется в `render` job kind с собственным воркером (образ без Playwright и
без LLM-клиентов, только LibreOffice). Проверка: `generate_docs.py` не знает
абсолютных путей; тест на put/get/delete с fs-бэкендом.

### M7 — Гейт П2 (API-only для клиентов)

`llm_client.py`: фоллбэк на CLI только для владельца; иначе `LLMOutageError`
→ `Job.status = deferred`, retry через `LLM_OUTAGE_PAUSE_MIN`. Тест:
mutation-verified (`.claude/skills/mutation-verify`): с чужим `user_id` CLI не
вызывается. Откат: env `CLI_FALLBACK_SCOPE=owner|all`.

### M8 — Слияние `apply_cli` в `core` (docs/quality/05)

После M3 CLI-путь становится **вариантом `LLMClient`** (генерация через
`claude -p`), а не вторым пайплайном; все стадии после генерации идут через
`core.tailor()`. `apply_cli.py` удаляется. Проверка: golden CLI E2E
(`tests/test_golden_apply_cli_e2e.py`) проходит на новом пути.

### M9 — Проекция `applications` и вывод SQLite (Stage 5–8 пивота)

Когда клиентские сценарии живут в Postgres, `applications` в SQLite
заполняется проекцией из `tailorings` для владельца (Sheets-зеркало и
`/funnel` продолжают работать), затем NestJS переключается на Postgres,
SQLite остаётся только как бэкап-снапшот. Проверка: `/funnel` и
`gsheets_sync` дают те же числа из проекции, что из старой таблицы.

---

## Risks

| Риск | Чем ловится |
|---|---|
| M2 фасад изменит значение какой-то константы (например, тип bool из строки) | Тест 100% имён + тип + значение при пустом env |
| M3 сломает порядок стадий | Golden E2E API и CLI; байтовое сравнение content.json |
| Postgres на Stage 2 замедлит dogfooding | Бот остаётся на SQLite, мост только через HTTP |
| Два источника правды на время M4–M9 | `applications` объявлена read-model в `DOMAIN_MODEL.md`, писатели перечислены |
| Рендер-воркер усложнит отладку | Только по результату M0.3, не по умолчанию |

## Cost

Ни одного нового LLM-вызова. Инфраструктура: Postgres-контейнер на том же
VPS (RAM ~200 МБ), R2 позже (~$0 при текущем объёме).

## Open questions

1. Postgres на Stage 2, а не Stage 8 (да/нет)?
2. Alembic в репо бота как единственный владелец DDL, NestJS перестаёт
   зеркалировать схему (да/нет)?
3. CLI-фоллбэк только для владельца (да/нет)?
4. `applications` в SQLite остаётся read-model владельца до Stage 5 (да/нет)?
