# Ops / SRE Plan — надёжность и эксплуатация

**Status:** draft
**Date:** 2026-09-09
**Роль:** приглашённый SRE / DevOps (аудит агентом, read-only, по трём репо)
**Motivation:** прод это один VPS, три compose-стека, деплой `pull && up -d`
без staging и без graceful stop. Для личного бота приемлемо; для клиентских
данных нет ни одного бэкапа реальной БД и документов.

---

## Текущая картина

Один VPS (Ubuntu 24.04, `deploy@`), общий Docker-демон с чужими проектами
(`docs/DEPLOY.md:643-648`). Три compose-стека без оркестрации между ними:

- **Bot**: `ghcr.io/igrdevelop/job-hunter:${IMAGE_TAG}` (`docker-compose.yml:5`),
  `python -m hunter` (`Dockerfile:37`), один процесс = Telegram polling + PTB
  JobQueue (все расписания, `hunter/schedules/__init__.py:68-262`) + фоновый
  `apply_worker_loop` (`hunter/telegram_bot.py:190`) + apply как subprocess.
  Образ `python:3.11-slim` + LibreOffice + nodejs/npm + Claude CLI + Chromium
  (`Dockerfile:7-30`), ~2 GB compressed.
- **API**: NestJS, `node:22-alpine`, порт `127.0.0.1:3000`; bind-mount живых
  bot-данных: `db/` rw, `users/` rw, `.env` ro (`api/docker-compose.prod.yml:36-40`).
  `cloudflared` в том же стеке, единственный вход.
- **Site**: nginx static, деплоится в compose-файл, который пишет только API-репо.

Деплой везде одинаков: push в `master` → build+push GHCR `:latest`+`:sha` → ssh
→ `compose pull && up -d`. Staging нет. Rollback только ручной
`IMAGE_TAG=<sha> docker compose up -d`, образы старше недели вычищаются.
Zero-downtime нет. Healthcheck в compose отсутствует, `restart: always`.

**In-flight apply при рестарте.** PTB `Application.stop()` ждёт
`job_queue.stop(wait=True)` и все created tasks; `apply_worker_loop` это
бесконечный `while True` (`hunter/apply_worker.py:183`), graceful stop не
завершается, Docker через 10 с шлёт SIGKILL (нет `stop_grace_period`). Ветка
`CancelledError → proc.kill()` (`apply_service.py:205-211`) не срабатывает.
Строка остаётся `IN_PROGRESS`: первый sweep через 15 мин после старта, но
снимает только claims старше `APPLY_CLAIM_TIMEOUT_MIN=60`. Итого 60–75 мин
простоя для убитой вакансии; `generate_docs.py` пишет tracker-строку ДО PDF
(`:429` vs `:538`), возможна APPLIED-строка над папкой без документов.

| Аспект | Сейчас | Риск для SaaS | Файл |
|---|---|---|---|
| Бэкап tracker.db | **Нет.** Ежедневно копируется `tracker.xlsx`, который в проде пишется только по `/export` | **high** | `hunter/tracker_backup.py:49`, `hunter/config.py:234` |
| Бэкап app.sqlite (users, auth, profiles) | Нет | **high** | `api/docker-compose.prod.yml:22,37` |
| Бэкап `users/` (CV, профили) и `Applications/` | Нет; Drive-upload best-effort, только владелец | **high** | `hunter/gdrive_sync.py` |
| Off-host копия | Только `hunter_errors.log` на Drive в 06:10 | high | `hunter/schedules/gdrive.py:31-45` |
| Два процесса на одном SQLite | WAL, каталог-mount; Python без явного `timeout` (default 5 с), Nest `busy_timeout=5000`; `synchronous=NORMAL` | med | `hunter/db.py:174-178`, `api/src/tracker/tracker.service.ts:60-61` |
| Две схемы-владельца tracker.db | И бот (`init_db`), и API (`tracker-migrations.ts`) делают `ALTER/CREATE` | med | `hunter/db.py:349-380`, `api/src/db/tracker-migrations.ts:21-80` |
| Graceful shutdown | Невозможен; SIGKILL через 10 с | high | `hunter/apply_worker.py:183` |
| Логи | stdout + `RotatingFileHandler` 5 MB×10, `apply_stdout/` 7 дней, `apply_failures.jsonl`, `dual_shadow/`; json-file 10m×5 | low/med | `hunter/__main__.py:22-45`, `docker-compose.yml:52-56` |
| Алерты | Только Telegram владельца: `best_effort` (3 подряд, cooldown 6 ч), `source_health`, `oauth_alert`, deploy-fail | med | `hunter/best_effort.py:51,151`, `deploy.yml:174-183` |
| Health | `/health`, `/status` в Telegram; API `GET /health` возвращает `ok` без проверки БД/диска | med | `api/src/health/health.controller.ts:8-10` |
| Метрики/трейсинг | Нет. Метеринг возможен из `cost_usd` (пусто в CLI-режиме) + `user_id` | med | `hunter/tracker.py` |
| Ресурсные лимиты | Нет `mem_limit`/`cpus`; LibreOffice+Chromium+Node в одном контейнере | high при 10× | `Dockerfile`, compose |
| Конкурентность | 1 apply-worker, `_hunt_lock` FIFO, `_DRIVE_LOCK` на каждый вызов, источники последовательно | high при 10× | `hunter/main.py:56,188`, `hunter/gdrive_sync.py:97` |
| LibreOffice | `subprocess.run(soffice --headless)` timeout 120 с, холодный старт на каждую папку | med | `generate_docs.py:413-418` |
| Конфиг | 103 `os.getenv` в `config.py`; в `.env.example` 60, **55 отсутствуют** (`APPLY_QUEUE_ENABLED`, `DEFAULT_USER_ID`, `USERS_ROOT`, `TRACKER_DB_PATH`, `GDRIVE_ENABLED`, все `*_ENABLED`…) | med | `hunter/config.py`, `.env.example` |
| Секреты | `.env`, OAuth-токены, `.claude-cli/` файлы на хосте, bind-mount; API `.env` перезаписывается из GH secrets на каждый деплой | med | `docker-compose.yml:25-43`, `api/deploy.yml:104-116` |
| Планировщик | Все cron-задачи это PTB JobQueue внутри Telegram-процесса | high для сервиса | `hunter/schedules/__init__.py` |
| CI | ruff + pytest гейтят деплой; mypy `continue-on-error`; Sonar выключен; единый lock | low | `deploy.yml:16-66,102-104` |

## Что уже сделано хорошо

- Атомарная очередь apply: `claim_pending` через `UPDATE…RETURNING`, `release_claim`, `reset_stale_claims` (`hunter/tracker.py:1430-1497`, `hunter/schedules/apply_queue.py`).
- `best_effort()` со счётчиками в SQLite, переживающими границу subprocess, + recovery-сообщение (`hunter/best_effort.py`, `docs/quality/03`).
- `source_health`: детект «сломался vs тихий день».
- Уроки инцидентов закреплены в коде: `set -eo pipefail` + проверка диска перед pull (`deploy.yml:139-169`), каталог-mount для WAL-сайдкаров, `test -s` для compose-файла, host-cron на prune.
- Образы тегируются `:sha`; rollback технически возможен неделю.
- Транскрипт stdout каждого apply-run 7 дней: база для post-mortem.
- Post-deploy smoke E2E против прода (`site/.github/workflows/smoke.yml`).
- SaaS-план фиксирует правильную цель: сервис как единственный исполнитель, outbox, `user_id` из NestJS.

---

## M0 — Measure ($0, read-only, на хосте)

```bash
docker stats --no-stream                       # в спокойный час и во время apply
ls -la db/ backups/                            # ожидаемо: только tracker_*.xlsx старой даты
du -sh users/ logs/
sqlite3 db/tracker.db "select ats_status,count(*) from applications where ats_status in ('PENDING','IN_PROGRESS') group by 1"   # сразу после деплоя
grep -c "database is locked" logs/hunter_errors.log*
docker inspect job-hunter | grep -i StopTimeout
```

Правила: RSS bot-контейнера > 50% RAM VPS при одном apply → M8 обязателен до
второго клиента; ≥ 1 `IN_PROGRESS` старше 15 мин после деплоя → M3
обязателен; `database is locked` > 0 за неделю → поднять busy_timeout в M2.

## M1..Mn — Milestones

### M1 — Бэкап реальных данных (durability first)

`hunter/tracker_backup.py` копирует tracker.db через
`sqlite3.Connection.backup()` (не `shutil.copy2`, WAL), плюс app.sqlite; новый
host-cron `restic backup /home/deploy/job-hunter/{db,users}
/home/deploy/job-hunter-web/app-data` на S3-совместимый бакет (Backblaze
B2 / Hetzner Object Storage) с `restic forget --keep-daily 30 --keep-weekly 12`.
Litestream для двух SQLite уместен (дёшево, непрерывная репликация,
sidecar), но не заменяет restic для `users/`. Проверка: `restic snapshots`,
тестовый `restic restore` в tmp + `pragma integrity_check`. Rollback: удалить
cron. Связь: `07-COMPLIANCE_PLAN.md` требует, чтобы erasure доходило и до
бэкапов (retention бэкапов ≤ 30 дней это и есть механизм).

### M2 — Согласовать SQLite между двумя процессами

`sqlite3.connect(..., timeout=15)` в `hunter/db.py:174`; `busy_timeout` в
`user.db.ts`/`profile.db.ts`; один владелец DDL: API перестаёт делать
`ALTER TABLE applications`, бот единственный мигратор, API проверяет
`PRAGMA user_version` и падает с понятной ошибкой. Проверка: тест на
параллельную запись из двух процессов. Это временная мера до Postgres
(`03-ARCHITECTURE_PLAN.md` П1/П3).

### M3 — Рестарт без потерь

`stop_grace_period: 120s` в compose; в `apply_worker_loop` `asyncio.Event`
остановки, `post_shutdown`-хук PTB с `release_claim` для активной вакансии;
при старте `reset_stale_claims(0)` для строк, которые claim'ил ЭТОТ процесс
(`claimed_by` = hostname/pid). `generate_docs.py`: tracker-строка после PDF
или пометка «rendering» до `convert_all_to_pdf`. Проверка:
`docker compose restart` во время apply → строка вернулась в PENDING ≤ 1 мин,
дубль-папки нет. Rollback: env-флаг.

### M4 — Dead-man switch и health

healthchecks.io-style ping из `scheduled_daily_summary` и после каждого
успешного цикла worker; compose `healthcheck` для API (`GET /health`
расширить: `SELECT 1` на обеих БД + свободное место); Telegram-алерты
перестают быть единственным каналом (email через существующий SMTP).
Проверка: остановить бот → ping-сервис алертит через 2 интервала.

### M5 — Структурные логи и метеринг

JSON-formatter в `hunter/__main__.py:_setup_logging` с полями `user_id`,
`url`, `stage`, `duration`, `cost_usd`; Sentry (или self-hosted GlitchTip, см.
открытый вопрос 7) для Python и Nest. Prometheus+Grafana и Loki на 1 VPS/10
клиентов overkill; вместо этого таблица `usage_events(user_id, kind,
cost_usd, model, ts)` (совпадает с `generation_runs` из `08-DATA_EVAL_PLAN.md`
M1, делать одной таблицей), куда пишут `tracker.set_cost` и CLI-путь (сейчас
CLI-run даёт пустой `cost_usd`; считать по токенам из `llm_client` даже для
CLI). Проверка: SQL по `usage_events` совпадает с суммой `cost_usd`.

### M6 — Конфиг и секреты

Закрыть 55 пропусков в `.env.example` (тест-гейт: getenv-имена ⊆
`.env.example`, по образцу `tests/test_handoff_readiness.py` проверка (b));
`pydantic-settings` (совместно с `03-ARCHITECTURE_PLAN.md` M2); секреты через
sops-encrypted файл или docker secrets; per-tenant настройки только в БД.

### M7 — Выделение планировщика и apply из Telegram-процесса

По `03-ARCHITECTURE_PLAN.md` M4: `hunter/schedules/*` → APScheduler в
контейнере сервиса; `apply_worker_loop` переезжает туда же и получает N
воркеров; Telegram тонкий клиент; `_hunt_lock` per-tenant. Порядок: сначала
apply-worker (у него уже DB-очередь, PTB нужен только для `send_text` →
outbox), потом расписания.

### M8 — Масштабирование рендера

LibreOffice в отдельный контейнер (`gotenberg` или `unoserver`): сейчас каждый
apply стартует soffice с нуля, 5–15 с холодного старта и 300–500 MB RSS; при
10 клиентах × 6 apply/день очередь и OOM. Playwright тоже отдельный образ или
`browserless`, только для Inhire/LinkedIn-session. `mem_limit` на каждый
контейнер. Проверка: `docker stats` при 3 параллельных apply; p95 render < 30 с.

CI-дополнение: job-summary с временем pytest; `pytest -n auto`
(`04-ENGINEERING_PLAN.md` M3).

---

## Risks

| Риск | Чем ловится |
|---|---|
| restic на хосте без мониторинга сам тихо сломается | M4 dead-man ping из бэкап-скрипта |
| `stop_grace_period` 120 с удлинит деплой | Приемлемо; см. открытый вопрос 4 |
| Один владелец DDL ломает независимость деплоев двух репо | Временно до Postgres; `user_version` даёт понятную ошибку вместо тихого дрейфа |

## Cost

Object storage для restic ~$1–3/мес; healthchecks.io бесплатный тир; Sentry
бесплатный тир либо GlitchTip на том же VPS.

## Open questions

1. Есть ли у провайдера VPS снапшоты диска и включены ли они?
2. Устроит ли Backblaze B2 / Hetzner Object Storage как хранилище restic?
3. Сколько RAM/CPU на VPS и есть ли бюджет на второй хост (staging/worker)?
4. Принять 1–2 минуты недоступности бота при деплое ради graceful stop (да/нет)?
5. Метеринг для биллинга по `cost_usd` (API) или по токенам всегда, включая CLI?
6. Отделить канал system-алертов (`TELEGRAM_CHAT_ID`) от клиентских уведомлений уже в M4 (да/нет)?
7. Sentry (данные наружу) допустим для трейсбеков с текстом вакансий/CV, или self-hosted GlitchTip?
8. DDL tracker.db владеет только бот, API падает при несовпадении `user_version` (да/нет)?
