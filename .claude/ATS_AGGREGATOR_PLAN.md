# ATS Aggregator — план реализации

**Status:** Phase 1 ✅ DONE (Workable + Netguru live, 3 vacancies passing prefilter on first run, 2026-04-26). Phases 2–4 — pending.
**Owner:** Ihar
**Цель:** добавить универсальный источник вакансий, который читает career-страницы компаний через публичные JSON API популярных ATS-систем (Workable, Greenhouse, Lever, Recruitee, Ashby).

---

## 1. Зачем

Многие польские/EU студии (Netguru, Apptension, …) и стартапы **не публикуют вакансии на pracuj.pl / justjoin.it**, особенно senior и нишевые роли. Их вакансии живут только на собственной `/careers` странице, которая под капотом — iframe или редирект на ATS (Workable / Greenhouse / Lever / Recruitee / Ashby).

У всех этих ATS есть **публичный JSON API**, одинаковый для всех клиентов конкретного провайдера. Один скрапер на провайдера → читаем вакансии любой компании, у которой этот провайдер. Добавить новую компанию = одна строка в конфиге, без нового файла.

**Польза:**
- Доступ к вакансиям, которых нет на агрегаторах (Pracuj/JustJoin)
- Масштабируется без копипасты кода
- JSON стабильнее, чем HTML-парсинг (меньше поломок)

---

## 2. Архитектура

### Высокоуровнево

```
hunter/sources/ats_aggregator.py    — единый Source, итерируется по списку компаний
hunter/ats/
    __init__.py
    base.py                         — ATSProvider ABC: fetch(slug) → list[Job]
    workable.py                     — Workable adapter
    greenhouse.py                   — Greenhouse adapter
    lever.py                        — Lever adapter
    recruitee.py                    — Recruitee adapter
    ashby.py                        — Ashby adapter
hunter/ats_companies.json           — список компаний {slug, provider, tags}
job_fetch/ats.py                    — fetcher для деталей вакансии (использует тот же провайдер)
```

### Поток данных

1. `AtsAggregatorSource.search()` читает `ats_companies.yml`
2. Для каждой записи `(slug, provider)` зовёт соответствующий `ATSProvider.fetch(slug)`
3. Каждый адаптер бьёт публичный API провайдера → нормализует в `Job(title, company, location, url, source="ats:workable:netguru", …)`
4. Результаты сливаются, идут в общий `filters.apply_filters()` пайплайн
5. Для деталей: `job_fetch/__init__.py` распознаёт URL по домену (apply.workable.com / boards.greenhouse.io / jobs.lever.co / *.recruitee.com / jobs.ashbyhq.com) и зовёт `job_fetch/ats.py`

### Принципы

- **Один Source, не один на провайдера.** Все ATS-вакансии идут под общим `AtsAggregatorSource` — так сохраняется единый toggle, расписание, дедуп. Различение по полю `source="ats:workable:netguru"`.
- **Конфиг отдельно от кода.** Список компаний в YAML/JSON, чтобы добавлять без правок Python.
- **Graceful degradation.** Падение API одной компании не должно валить весь источник — лог + skip.

---

## 3. API провайдеров (справочник)

### 3.1 Workable
- **List jobs:** `GET https://apply.workable.com/api/v3/accounts/{slug}/jobs`
- **Job detail:** `GET https://apply.workable.com/api/v3/accounts/{slug}/jobs/{shortcode}`
- **Auth:** не требуется
- **Pagination:** курсорная (`?since_id=...`) — для большинства компаний помещается в один запрос
- **Поля:** `title`, `shortcode`, `state`, `department`, `location.country`, `location.city`, `application_url`
- **URL вакансии:** `https://apply.workable.com/{slug}/j/{shortcode}/`
- **Пример:** netguru, brainhub

### 3.2 Greenhouse
- **List jobs:** `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs`
- **Job detail:** `GET https://boards-api.greenhouse.io/v1/boards/{slug}/jobs/{id}?content=true`
- **Auth:** не требуется
- **Pagination:** нет, всё за один запрос
- **Поля:** `id`, `title`, `location.name`, `absolute_url`, `updated_at`, `departments[]`
- **URL вакансии:** `absolute_url` из ответа
- **Пример:** stripe, airbnb, разные стартапы

### 3.3 Lever
- **List jobs:** `GET https://api.lever.co/v0/postings/{slug}?mode=json`
- **Job detail:** `GET https://api.lever.co/v0/postings/{slug}/{id}?mode=json`
- **Auth:** не требуется
- **Поля:** `id`, `text` (title), `categories.location`, `categories.team`, `hostedUrl`, `createdAt`, `descriptionPlain`
- **URL вакансии:** `hostedUrl`
- **Пример:** netflix, miro

### 3.4 Recruitee
- **List jobs:** `GET https://{slug}.recruitee.com/api/offers/`
- **Job detail:** `GET https://{slug}.recruitee.com/api/offers/{id}`
- **Auth:** не требуется
- **Поля:** `offers[].id`, `title`, `location`, `country_code`, `careers_url`, `description`
- **URL вакансии:** `careers_url`
- **Пример:** apptension

### 3.5 Ashby
- **List jobs:** `GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true`
- **Job detail:** в том же ответе `descriptionHtml` уже есть
- **Auth:** не требуется
- **Поля:** `jobs[].id`, `title`, `locationName`, `employmentType`, `jobUrl`, `descriptionHtml`
- **URL вакансии:** `jobUrl`
- **Пример:** linear, posthog

---

## 4. Конфиг — `hunter/ats_companies.json`

```json
{
  "companies": [
    {
      "slug": "netguru",
      "provider": "workable",
      "tags": ["poland", "outsourcing"]
    },
    {
      "slug": "apptension",
      "provider": "recruitee",
      "tags": ["poland", "outsourcing"]
    }
  ]
}
```

`provider` принимает одно из: `workable | greenhouse | lever | recruitee | ashby`.
`slug` — идентификатор компании внутри этого ATS (см. §3).
`tags` — необязательное поле, подсказки для будущих фильтров/диагностики.

---

## 5. Фазы реализации

### Phase 1 — MVP на одном провайдере (Workable + Netguru) ✅ DONE
**Цель:** доказать, что схема рабочая, end-to-end до Telegram-карточки.

**Реализовано (2026-04-26):**
- `hunter/ats/base.py` — `ATSProvider` ABC
- `hunter/ats/workable.py` — Workable widget API (`apply.workable.com/api/v1/widget/accounts/{slug}`, не legacy `/api/v3/accounts/{slug}/jobs` — он возвращает 404 для ACP-аккаунтов)
- `hunter/ats_companies.json` — содержит netguru
- `hunter/sources/ats_aggregator.py` — `AtsAggregatorSource`, диспатч по `provider`
- `hunter/config.py` — добавлены `ATS_AGGREGATOR_ENABLED` и `ATS_COMPANIES_PATH`
- `hunter/sources/__init__.py` — регистрация
- `job_fetch/ats_workable.py` + домен `apply.workable.com` в роутере
- `.env.example` — `ATS_AGGREGATOR_ENABLED=true`
- `tests/test_ats_workable_parse.py` — 9 тестов, все green
- Smoke-run: 3 фронтенд-вакансии Netguru попали в листинг
- `CLAUDE.md` обновлён

Шаги:
1. Создать `hunter/ats/__init__.py`, `hunter/ats/base.py` (ABC `ATSProvider`)
2. Реализовать `hunter/ats/workable.py`:
   - метод `fetch(slug) -> list[Job]`
   - нормализация: `title`, `company` (берём из ответа или передаём отдельно), `location`, `url`, `source = f"ats:workable:{slug}"`, `posted_at`
3. Создать `hunter/ats_companies.yml` с одной записью `netguru/workable`
4. Создать `hunter/sources/ats_aggregator.py`:
   - `AtsAggregatorSource(BaseSource)`
   - читает YAML, диспатчит по провайдерам, сливает результаты
5. Конфиг в `hunter/config.py`:
   - `ATS_AGGREGATOR_ENABLED = env_bool("ATS_AGGREGATOR_ENABLED", default=True)`
   - `ATS_COMPANIES_PATH = REPO_ROOT / "hunter" / "ats_companies.yml"`
6. Регистрация в `hunter/sources/__init__.py`
7. Добавить в `.env.example`: `ATS_AGGREGATOR_ENABLED=true`
8. Добавить в расписание (`hunter/config.py SOURCE_SCHEDULE`) — слот после `remoteleaf`
9. Детальный fetcher: `job_fetch/ats_workable.py` + регистрация в `job_fetch/__init__.py` (домены `apply.workable.com`)
10. Проверить руками: `python -c "from hunter.sources.ats_aggregator import AtsAggregatorSource; print(AtsAggregatorSource().search()[:3])"`
11. Тест: `tests/test_ats_workable_parse.py` — мок ответа Workable, проверка нормализации

**Критерий готовности Phase 1:** `/hunt ats_aggregator` в Telegram присылает Netguru-вакансии.

### Phase 2 — добавить остальные провайдеры
- `hunter/ats/greenhouse.py`
- `hunter/ats/lever.py`
- `hunter/ats/recruitee.py`
- `hunter/ats/ashby.py`
- Соответствующие fetcher'ы в `job_fetch/`
- Тесты на парсинг для каждого (мок-ответы)

### Phase 3 — наполнить список компаний
Кандидаты для `ats_companies.yml` (требуют верификации — у какой компании какой ATS):

**Польские студии:**
- netguru → workable ✅ (verified)
- apptension → recruitee ✅
- brainhub → ? (проверить)
- 10clouds → ?
- monterail → ?
- ulam labs → ?
- itmagination → ?
- spyrosoft → ?
- merixstudio → ?
- xfive → ?

**EU/remote-friendly стартапы:**
- gitlab → greenhouse
- hashicorp → greenhouse
- vercel → greenhouse
- linear → ashby
- posthog → ashby
- miro → lever

Способ верификации slug: открыть карьерную страницу, посмотреть в DevTools → Network, какой домен отвечает на список вакансий.

### Phase 4 — улучшения (по желанию)
- Кэш списка вакансий (избегать долбить API при частых запусках) — Redis/файловый кэш с TTL=1h
- Метрики: сколько вакансий с какого провайдера / компании (через telegram `/status`)
- Авто-проверка «жива ли компания»: если slug возвращает 404 N раз подряд — пометить в YAML как `disabled: true`
- Тег `priority: high|low` в YAML — для приоритезации в листинге

---

## 6. Edge cases / известные риски

- **Локация в ATS часто свободный текст** (`"Remote"`, `"Wrocław, Poland"`, `"EU"`) — фильтр локации (`hunter/filters.py`) должен это переваривать. Нужно проверить на реальных данных, возможно расширить regex.
- **Pagination для крупных компаний** (Stripe = сотни вакансий) — Workable требует курсор. В MVP можно ограничиться первой страницей (≤100), но честно реализовать в Phase 2.
- **Rate limits.** Публичные ATS API мягкие, но если в YAML 100 компаний × 3 запуска/день = 300 req — это нормально. Если 1000 компаний — добавить asyncio + semaphore.
- **Дублирование с другими источниками.** Та же netguru-вакансия может появиться и на pracuj.pl, и через ATS. Уже работающий dedup по URL не сработает (URLs разные). Дедуп по `(company, title)` — работает (есть в `hunter/main.py`). Проверить что покрывает.
- **Языки.** Recruitee у польских компаний может отдавать `title` на польском. ATS-аггрегатор не должен фильтровать по языку — это работа `filters.py`.
- **Архивные вакансии.** У Workable бывает `state: "archived"` — фильтровать только `state == "published"`. Аналог в каждом провайдере.

---

## 7. Что НЕ делать

- Не писать отдельный `Source` для каждой компании (это противоречит идее).
- Не лезть в HTML career-страниц компаний — если у компании нет публичного ATS API, она просто не подходит для этого модуля.
- Не добавлять Playwright — все эти API отдают чистый JSON.
- Не парсить email-адреса/контакты из ATS — мы только ищем вакансии.

---

## 8. Acceptance criteria (DoD)

- [ ] Phase 1: Netguru-вакансии приходят в Telegram через `/hunt ats_aggregator`
- [ ] Тесты `tests/test_ats_*.py` проходят
- [ ] `python -m compileall .` без ошибок
- [ ] `.env.example` обновлён
- [ ] `CLAUDE.md` обновлён (новая папка `hunter/ats/`, новый источник, новый файл `ats_companies.yml`)
- [ ] `JOB_SOURCES_ROADMAP.md` обновлён — отметить ATS как реализованный

---

## 9. Решения (подтверждены 2026-04-26)

1. **Формат конфига:** JSON (`hunter/ats_companies.json`). Без новых зависимостей.
2. **Тогл:** один общий — `ATS_AGGREGATOR_ENABLED`.
3. **Расписание:** автоматическое — `SCHEDULE_SOURCE_OFFSET_MIN` сам распределит новый источник по слотам. Дополнительной настройки не требуется.
4. **Хранение конфига:** в репозитории под git.

---

## 10. Контекст для агента, который это реализует

**Этот файл — твой брифинг.** Чтобы войти в задачу:

1. Прочитай `CLAUDE.md` (общая архитектура проекта) и `.claude/JOB_SOURCES_ROADMAP.md`
2. Прочитай `hunter/sources/base.py` — интерфейс `BaseSource`
3. Прочитай `hunter/sources/remotive.py` — самый простой пример Source на JSON API (хороший шаблон)
4. Прочитай `hunter/sources/__init__.py` — как регистрируются источники
5. Прочитай `hunter/config.py` — как добавлять toggle и расписание
6. Прочитай `job_fetch/__init__.py` + `job_fetch/remotive.py` — как добавляется detail fetcher
7. Реализуй Phase 1 (MVP на Workable + Netguru), останавливаясь после каждого шага из §5 и проверяя `python -m compileall .`
8. После Phase 1 — спроси у Ihar'а: продолжать на Phase 2 или сначала погонять MVP неделю
