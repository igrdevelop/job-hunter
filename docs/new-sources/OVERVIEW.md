# New Remote Sources — Overview

> Источник идеи: PDF «13 сайтов с remote вакансиями» (Майя Литвина).
> Цель: расширить пул источников за счёт глобальных remote-first досок.
> Кандидат готов работать **на компании за пределами Европы** → US/worldwide-крен
> больше не является минусом, фильтруем только по стеку и наличию вакансий.

Ветка: `feat/new-remote-sources` (от свежего `origin/master`).

---

## Сводка: 13 сайтов из PDF

| # | Сайт | Статус у нас | Решение |
|---|------|--------------|---------|
| 01 | Wellfound (ex-AngelList) | нет | Очередь 3 |
| 02 | Working Nomads | нет | **Очередь 1** |
| 03 | #Web3 Jobs | нет | СКИП (только блокчейн) |
| 04 | RemoteOK | ✅ `remoteok.py` | — |
| 05 | We Work Remotely | ✅ `weworkremotely.py` | — |
| 06 | Jobspresso | нет | **Очередь 1** |
| 07 | FlexJobs | нет | СКИП (paywall) |
| 08 | JustRemote | нет | Очередь 2 |
| 09 | StillHiring.Today | нет | СКИП (список компаний, не доска) |
| 10 | Jobgether | нет | Очередь 3 |
| 11 | Built In | нет | Очередь 2 |
| 12 | Remote.co | нет | Очередь 2 |
| 13 | Remotive | ✅ `remotive.py` | — |

**Уже есть:** 3. **Добавляем:** 7. **Скип:** 3.

---

## Очереди (по возрастанию сложности / убыванию ROI)

### Очередь 1 — «бесплатные» (JSON API + RSS, шаблоны уже в коде)
- **Working Nomads** — публичный JSON (`/api/jobs`)
- **Jobspresso** — RSS-фид (WP Job Manager)

Минимальный риск, та же механика, что Remotive/SolidJobs/WWR. Деталь см. `QUEUE-1-easy.md`.

### Очередь 2 — средний effort (HTML-скрейп, возможен Cloudflare)
- **Built In** — максимум frontend-объёма; HTML + JSON-LD
- **JustRemote** — remote-dev; HTML, возможен `cloudscraper`
- **Remote.co** — небольшой, чистый; HTML/листинг

Деталь см. `QUEUE-2-medium.md`.

### Очередь 3 — высокий объём, тяжёлый парсинг (берёмся, если 1–2 мало дают)
- **Wellfound** — GraphQL за бот-защитой
- **Jobgether** — SPA, нужен Playwright (а он в Docker сейчас не работает, см. ниже)

Деталь см. `QUEUE-3-hard.md`.

---

## Текущая архитектура источника (как добавлять — актуально на 2026-06)

> ⚠️ Файл `.claude/commands/add-source.md` УСТАРЕЛ — он ещё описывает удалённый
> пакет `job_fetch/`. Реальная архитектура после Phase 3 рефактора:

Каждый источник — один файл `hunter/sources/{site}.py`, подкласс `BaseSource`
(`hunter/sources/base.py`), реализует:

1. **`search() -> list[Job]`** — обязательный. Тянет листинг, возвращает `Job`.
   Без фильтрации/дедупа (это централизованно в `hunter/main.py`). Сам глотает
   сетевые ошибки и возвращает `[]` при сбое. Внутри использует
   `self.matches_coarse_prefilter(title, context_text)` для раннего отсева шума.
2. **`matches_url(url) -> bool`** — вернуть True для своего домена (по `urlparse`).
3. **`fetch_text(url) -> str`** — полный текст вакансии для LLM. Если у сайта нет
   структурированных данных, можно не переопределять — сработает дефолтный
   `html_fallback.fetch_html`. Но переопределить желательно (чище текст).

`Job` (см. `hunter/models.py`): `title, company, location, salary, url, source, raw`.
Всегда ставить `source=self.name`.

### Шаги интеграции (на каждый источник)
1. `hunter/sources/{site}.py` — класс `{Site}Source(BaseSource)`, `name="{site}"`.
2. `hunter/config.py` — добавить тогл:
   ```python
   {SITE}_ENABLED: bool = os.getenv("{SITE}_ENABLED", "true").lower() in ("true", "1", "yes")
   ```
3. `hunter/sources/__init__.py` — три правки:
   - импорт тогла в шапке;
   - блок `if {SITE}_ENABLED: ... ALL_SOURCES.append({Site}Source())`;
   - добавить инстанс в `_fetch_roster()` (чтобы `fetch_job_text` мог тянуть
     детально даже при выключенном тогле).
4. Тест-файл `tests/test_source_{site}.py` — парсинг из зафиксированного
   сэмпла (HTML/JSON/RSS), `matches_url`, отсев нерелевантных. Сетевые вызовы
   мокать (как в существующих `tests/test_source_*`).
5. `CLAUDE.md` — строка в таблице «Job Sources», в «Repository Layout», в
   «Scraper Health Notes», запись в «Agent Work Log». Тоглы в списке.

### Фильтр (`hunter/config.py` → `FILTER`)
- `title_keywords` — что считаем релевантным (frontend/angular/react/...).
- `exclude_patterns` — что режем по тайтлу.
- `locations` — `"remote"` всегда проходит (см. config.py:131). Для глобальных
  досок ставим `location="Remote"` / `"<region> (Remote)"`, как в Remotive.

---

## Сквозные соображения

### Дедуп и шум
Одна вакансия часто висит на нескольких глобальных досках (WWR ↔ Remotive ↔
Wellfound). Централизованный дедуп ловит по `normalize_url` **и** `company+title`
(`dedup_key`), но через разные домены URL не совпадает, а `company+title` ловит
не всегда (разные написания компании). При +7 источниках вырастет поток дублей в
Telegram. **План:** после Очереди 1 неделю понаблюдать за дублями; при росте —
усилить `dedup_key` (нормализация company, fuzzy по title).

### Объём и расписание
Каждый источник получает слот в расписании со смещением
`SCHEDULE_SOURCE_OFFSET_MIN` (40 мин). 18 → 25 источников растянет полный цикл.
Возможно стоит уменьшить offset или сгруппировать «лёгкие» JSON/RSS-источники.
Решение принять после Очереди 1 (замерить реальное время цикла).

### Playwright в Docker (блокер для Очереди 3 / Jobgether)
См. CLAUDE.md «Known Issues #4»: Playwright не установлен в Docker, Inhire поэтому
возвращает `[]`. Jobgether из Очереди 3 потребует того же. Прежде чем браться за
Jobgether — решить вопрос с Playwright в образе (+~500 МБ) ИЛИ найти у него
backend JSON-эндпоинт (DevTools → XHR).

### Rate limiting
Для HTML-источников с Cloudflare (JustRemote) переиспользовать `cloudscraper`
(уже в зависимостях) и при необходимости `hunter/rate_limiter.py` `DomainLimiter`
(как сделано для pracuj). Не молотить листинги — пара запросов на цикл.

### Terms of Service
Remotive/RemoteOK требуют атрибуции и не «хаммерить» API. Перед каждым новым
источником проверить его ToS/robots — для лёгких источников (Очередь 1) указать
в докстринге, как сделано в `remotive.py`.

---

## Порядок работ (рекомендуемый)

1. **Очередь 1** целиком (Working Nomads + Jobspresso) — отдельный PR.
2. Понаблюдать за дублями и временем цикла ~неделю.
3. **Очередь 2** по одному источнику, отдельными PR (Built In → JustRemote → Remote.co).
4. **Очередь 3** — только если поток с 1–2 недостаточен; сперва решить Playwright.

Каждая очередь = свой PR от свежего `origin/master` (см. git-workflow: одна ветка
на PR; если master ушёл вперёд — новая ветка, не rebase).
