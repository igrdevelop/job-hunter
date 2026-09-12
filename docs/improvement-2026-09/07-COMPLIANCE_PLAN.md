# Compliance Plan — GDPR, скрейпинг, условия LLM-провайдеров

**Status:** draft
**Date:** 2026-09-09
**Роль:** приглашённый эксперт по технологическому комплаенсу (аудит агентом,
read-only, по трём репо). Это инженерная карта рисков с митигациями, не
юридическая консультация; перед первым платящим клиентом нужна проверка
юристом по польскому/EU праву.
**Motivation:** CV это персональные данные в ЕС; сегодня удаление пользователя
чистит не всё, активный LLM-профиль глобален (смена модели владельцем отправит
CV всех клиентов другому процессору), на сайте нет privacy/terms, а часть
корпуса собрана методами, слабо защищёнными при коммерческой перепродаже.

---

## Карта рисков

| # | Риск | Область | Файл:строка | Суть | Митигация |
|---|---|---|---|---|---|
| 1 | **high** | Право на удаление | `api/src/admin/admin.service.ts:43-60`, `api/src/profile/profile.service.ts:495-500` | `deleteUser` чистит `users`, `profiles`, `profile_revisions`, `profile_jobs`, дерево `users/{uid}/`. НЕ чистит: `tracker.db.applications` (company/title/URL/folder/to_learn), `telegram_links`, `user_settings`, `email_verification_tokens`, `logs/apply_stdout/*`, `logs/apply_failures.jsonl`, `backups/`. Self-service удаления нет, только `@Roles('admin')` | M1: `erase_user(uid)` на стороне бота + вызов из API; e2e-тест «ноль строк во всех таблицах и файлах» |
| 2 | **high** | Процессоры LLM | `hunter/llm_profiles.py:66-88`, `llm_client.py:511,546-547` | Профили `deepseek-*`/`glm-5.2` идут через OpenRouter к DeepSeek и Z.ai (КНР). Активный профиль глобальный DB-ключ (`/llm`), не per-user: смена владельцем модели молча отправляет CV всех клиентов в Китай. Нет DPA с OpenRouter; трансфер вне ЕС без SCC | M2: per-user allowlist провайдеров; для `current_user_id() != DEFAULT_USER_ID` только провайдеры с DPA (Anthropic/OpenAI commercial API) |
| 3 | **high** | Dual-apply shadow | `apply_agent.py:159-170`, `hunter/dual_apply.py:486-488` | `launch_detached` проверяет только глобальный `dual_enabled()`; гейта по пользователю нет. При `/dual on` CV клиента генерируется вторично на shadow-модели (`deepseek-v3` по умолчанию): второй процессор без ведома клиента, лишняя копия в `{Company}/{shadow}/` | M2: `_maybe_run_shadow` → no-op для non-owner |
| 4 | **high** | Claude CLI fallback | `llm_client.py:144-165,192-210,364-375`; `apply_agent.py:84-152` | Личная подписка Pro/Max обслуживает любой `LLMOutageError` для любого user_id, плюс `--cli`/`APPLY_USE_CLI` ведут весь пайплайн через `claude -p`. Consumer-terms Anthropic не для перепродажи третьим лицам; нет DPA, нет ZDR; usage не учитывается в `cost_usd` | M2: в `_cli_fallback_enabled()` гейт `current_user_id() != DEFAULT_USER_ID → False`; в `apply_agent.main` тот же гейт перед `main_cli`; для клиентов только API (exit 46 → «попробуйте позже») |
| 5 | **high** | Юр. тексты | `site/src/app/app.routes.ts:13-125` (privacy/terms/cookie нет), `api/src/auth/dto/register.dto.ts` | На сайте нет Privacy Policy/ToS; регистрация только email+password, без чекбокса согласия; `login()` не проверяет `email_verified` (`auth.service.ts:70-78`) | M4: `/privacy`, `/terms`, чекбокс при signup с `consent_version/at` в `users`; гейт `email_verified` на login |
| 6 | **high** | Tenant isolation | `hunter/tracker.py:42` (`_uid()`), `hunter/bot/apply_runner.py:289-292` | Изоляция держится на env `JOB_HUNTER_USER_ID` в сабпроцессе; в bot-процессе `_uid()` = владелец (детали и план в `05-SECURITY_PLAN.md` № 2/M2) | Тесты изоляции; `user_env()` обязателен для non-owner |
| 7 | **med** | Скрейпинг ToS | `hunter/sources/pracuj.py`, `theprotocol.py`, `builtin.py`, `jobleads.py` (cloudscraper); `linkedin.py:32,221-238` (guest API + Playwright с залогиненной сессией) | Обход Cloudflare и LinkedIn User Agreement §8.2: терпимо для личного пользования, слабая позиция при коммерческой перепродаже; LinkedIn-сессия это аккаунт владельца, бан = потеря личного аккаунта | M6: для платного продукта вход по URL/paste; сбор только JSON/RSS/ATS-API источников; LinkedIn-сессия owner-only |
| 8 | **med** | Aggregate tier | `docs/SAAS_PIVOT_PLAN…md:285-295` | Stage 6 продаёт статистику требований, не текст вакансий. Защищённее, но sui generis-права на БД (Dir. 96/9/EC) и ToS pracuj/theprotocol всё равно затрагиваются, особенно с cloudscraper | M6: агрегаты только из JSON/RSS/ATS-API; k-anonymity (≥N вакансий на ячейку); никогда не отдавать `job_posting.txt` |
| 9 | **med** | Prompt cache | `hunter/apply_api.py:424-434`, `llm_client.py:445` | `candidate_profile.md` в system-prompt с `cache_control: ephemeral`: CV кешируется у Anthropic (5 мин). В рамках commercial API допустимо, но должно быть в DPA/privacy | Упомянуть в Privacy Policy |
| 10 | **med** | Логи с ПД | `hunter/apply_stdout_log.py:27,58-65`, `hunter/apply_failures_log.py:119-125` | stdout-транскрипты (7 дней) содержат company/title/URL и вывод QA/judge с фрагментами CV; `apply_failures.jsonl` без срока | M3: uid-префикс в именах, erasure удаляет по uid |
| 11 | **med** | ПД третьих лиц | `hunter/contact_extract.py`, `hunter/outreach.py` | В `outreach.md` сохраняются имя/email/телефон рекрутера из вакансии: обработка ПД третьих лиц по legitimate interest | `OUTREACH_ENABLED` per-user, default off для клиентов; либо указать в Privacy |
| 12 | **med** | Retention | `hunter/config.py:250` (backups 90 файлов), `api/src/db/migrations.ts:65-71` (`profile_revisions` без лимита), `Applications/` без срока | Нет политики хранения CV/документов | M3: TTL `Applications/` 180 дней после `sent`, cap ревизий, срок для неактивных аккаунтов |
| 13 | **med** | Telegram как канал | `hunter/users.py:136-156`, `hunter/pipeline/notify.py` | PDF с CV уходят в Telegram (ОАЭ), процессор вне ЕС без DPA | Per-user opt-in `TELEGRAM_SEND_DOCS`, default off для клиентов; явно в Privacy |
| 14 | **low** | Google fence | `hunter/bot/apply_runner.py:134-138`; backfills `tracker.py:1554,1898` через `_uid()` | Fence есть, но на уровне вызывающего (`if user_id:`), не внутри `delivery.py`/`gsheets_sync`/`gdrive_sync` | Перенести проверку внутрь `deliver_apply_now`/`mirror_new_row`/`upload_*` |
| 15 | **low** | AI Act / прозрачность | site (grep «generated/LLM/AI» пуст) | Клиенту не сообщается, что CV генерирует LLM, а judge/refine меняют содержимое (stretch-раунд добавляет навыки в `to_learn`) | M4: disclosure на странице генерации; показывать `to_learn` и `judge_report.json` клиенту |
| 16 | **low** | Work authorization | `hunter/profile_schema.py:76` | Иммиграционный статус: не Art. 9, но чувствительно | Хранить только enum (EU/non-EU) |

## Инвентарь персональных данных

| Данные | Где лежат | Кто получает | Удаляется при erasure? |
|---|---|---|---|
| Email, bcrypt-пароль, role | `app.sqlite.users` | SMTP-провайдер (только email) | Да |
| Verification tokens | `app.sqlite.email_verification_tokens` | — | **Нет** |
| Структурированный профиль (ФИО, контакт, город, языки, work_authorization, работодатели, образование, роли) | `app.sqlite.profiles/profile_revisions`; `users/{uid}/candidate/*`; `tracker.db.profile_jobs.payload` (не очищается после `finish`, `profile_jobs.py:77`) | — | Да, кроме payload |
| Загруженные резюме | `users/{uid}/uploads/` | Anthropic Haiku (парсер) | Да |
| Сгенерированные CV/CL, `content.json`, `job_posting.txt`, `outreach.md`, `judge_report.json` | `users/{uid}/Applications/…` | Anthropic / OpenAI / OpenRouter (по глобальному профилю); Haiku для judge/verdict/translate/outreach; shadow → OpenRouter; CLI fallback → Anthropic consumer; Telegram (PDF); владелец → Drive/Sheets | Да, но не Drive/Sheets владельца |
| Строки трекера (company/title/URL/stack/folder/to_learn/cost/verdict, `pending_meta`) | `tracker.db.applications` | — | **Нет** |
| Telegram chat_id ↔ user_id, per-user settings | `tracker.db.telegram_links/user_settings` | Telegram | **Нет** |
| Транскрипты apply, fail-log, `hunter_errors.log` | `logs/*` | — | **Нет** (имена без uid) |
| Бэкапы `tracker.xlsx` | `backups/` | — | **Нет** |
| Job-текст/профиль в промптах | Anthropic prompt-cache 5 мин | Anthropic | Истекает сам |

## Что уже сделано хорошо

- Owner-only fence для Sheets/Drive/Gmail работает: `apply_runner.py:134-138`; чтение почты не продаётся вовсе (план §3.4).
- Per-user изоляция FS: `hunter/users.py:111-156`, `api/src/files/safe-path.ts`, `_resolve_user_relative_path`.
- Erasure осознан как одна операция и частично реализован (`admin.service.ts:43-60`, `docs/RESUME_PROFILE_STORE.md:142-148`).
- Нейтральные defaults вместо данных владельца в коде; CI-гейт `tests/test_handoff_readiness.py`.
- Fabrication-контроль как плюс под AI Act и добросовестность: `claim_judge.py`, `scrubs.py`, `text_repair.py`; stretch-раунды прозрачно пишут добавленные навыки в `to_learn`.
- Ограниченная ретенция логов (7 дней stdout, RotatingFileHandler).
- Throttling загрузок/preview (`profile.controller.ts:64-69, 91-92`); preview CV без LLM.

---

## M0 — Measure ($0, read-only)

`tools/pii_inventory.py --user <uid> --dry-run`: перечислить все места, где
встречается uid (tracker.db таблицы, app.sqlite, `users/{uid}/`, grep по
`logs/`, `backups/`). Правило: если список больше того, что чистит
`admin.deleteUser`, M1 обязателен до первого клиента. Уже известно ≥ 6
пропусков, значит M1 первым.

## M1..Mn — Milestones

### M1 — Erasure как одна операция

Бот: `hunter/erasure.py::erase_user(uid)`: `DELETE FROM
applications/telegram_links/user_settings/profile_jobs WHERE user_id=?`,
`shutil.rmtree(users/{uid})`, удаление транскриптов (после M3 по uid-префиксу).
API: `admin.deleteUser` вызывает бот через `profile_jobs.kind='erase'` (тот же
queue-паттерн) + чистит `email_verification_tokens`; self-service
`DELETE /api/auth/me` с re-auth. Проверка: e2e «после удаления COUNT(*)=0 во
всех таблицах, папка отсутствует». Бэкапы: retention ≤ 30 дней (`06-OPS_PLAN.md` M1)
это и есть срок, за который erasure доходит до копий; зафиксировать в Privacy.
Rollback: флаг `ERASURE_V2`.

### M2 — Per-user gating провайдеров (совпадает с `03-ARCHITECTURE_PLAN.md` П2)

`llm_client._cli_fallback_enabled()` и `apply_agent.main` → False/`main_api`
при `current_user_id() != DEFAULT_USER_ID`; `apply_agent._maybe_run_shadow` →
no-op для non-owner; `llm_profiles.get_active()` → для клиентов фиксированный
allowlist (`sonnet`, `gpt-4.1`), OpenRouter только владелец. Проверка: тесты
с `JOB_HUNTER_USER_ID`; mutation-verify гейта. Rollback: env
`CUSTOMER_PROVIDER_ALLOWLIST`.

### M3 — Retention

uid-префикс в именах `apply_stdout`/`dual_shadow` логов; TTL для
`Applications/` per-user (`user_settings.retention_days`, default 180); cap
`profile_revisions` (последние 20); очистка `profile_jobs.payload` после
`finish`; `backups/` без клиентских строк в `tracker.xlsx`-экспорте.
Проверка: scheduled-job тест с fake-clock.

### M4 — Privacy / Terms / Consent / AI-disclosure

Site: `/privacy`, `/terms`, чекбокс на signup, страница генерации с текстом
«CV создано LLM; проверено judge; добавленные навыки в To Learn». API:
`users.consent_version/consent_at`, login требует `email_verified`,
`RegisterDto.acceptTerms`. Тексты: черновик по шаблону, финал у юриста.

### M5 — DPA-чеклист

`docs/PROCESSORS.md`: Anthropic commercial API (DPA + субпроцессоры), OpenAI
API (DPA, no-training по умолчанию), SMTP-провайдер, VPS-хостинг (страна,
DPA), Telegram (нет DPA, только opt-in), Cloudflare (DPA есть).
OpenRouter/DeepSeek/Z.ai: исключить для клиентов либо SCC + явное согласие.

### M6 — Политика скрейпинга для aggregate tier

Агрегаты только из источников класса «official JSON/RSS/ATS-API» (JustJoin,
NoFluff, Arbeitnow, Remotive, Himalayas, FindMyRemote, SmartJobs, 4dayweek,
WWR, SolidJobs, Greenhouse/Lever/Ashby/Recruitee/Workable); cloudscraper-
источники и LinkedIn исключены из nightly precompute; хранить только счётчики
требований (role×region×skill, k≥10), никогда текст вакансии/название
компании. Реестр robots.txt/ToS по источникам в `docs/SOURCES_POLICY.md`.

---

## Risks

| Риск | Чем ловится |
|---|---|
| Erasure удалит строки, нужные дедупу владельца | Только `WHERE user_id=?`, владелец не удаляется |
| Allowlist провайдеров оставит клиентов без генерации при outage Anthropic | OpenAI в allowlist как второй провайдер с DPA |
| Юр. тексты написаны «по шаблону» | Явно помечены как черновик до юриста |

## Cost

Ноль LLM. Юрист: разовая консультация. Регистрация в UODO при необходимости.

## Open questions

Решено 2026-09-12 (см. README, «Решения владельца»):

1. ~~OpenRouter (DeepSeek/GLM) для клиентов, или только Anthropic/OpenAI под
   DPA?~~ **Остаётся как есть** — провайдер один на всех, каким бы ни был
   активный профиль. M2 (per-user gating) не выполняется. Риск принят
   осознанно и пересматривается перед первым ПЛАТЯЩИМ клиентом, когда
   появляется договорная сторона. На время concierge-теста действует
   операционное правило вместо кода: не переключать `/llm` на
   OpenRouter-профиль и держать `/dual off`, пока в системе есть чужие
   пользователи.

Открыто:
2. Продавать ли aggregate tier, учитывая, что часть корпуса собрана через cloudscraper/LinkedIn?
3. Доставка PDF в Telegram для клиентов opt-in, по умолчанию выключена (да/нет)?
4. Отключить `outreach.md` для клиентских аккаунтов (да/нет)?
5. Хранить `job_posting.txt` клиентов дольше 60 дней (нужно repost-gate) или обрезать по TTL?
6. Запуск invite-only (регистрация выключена, `auth.service.ts:47`) до завершения M1/M4 (да/нет)?
7. Владелец остаётся на CLI-подписке для себя, клиенты только API (да/нет)?
8. Регистрируем ли обработку в Польше (UODO) как sole proprietor?
