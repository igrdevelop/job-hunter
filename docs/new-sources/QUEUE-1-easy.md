# Очередь 1 — лёгкие источники (JSON API + RSS)

Цель: добавить два источника по уже существующим в коде шаблонам, без новых
зависимостей и без риска Cloudflare. Один общий PR.

Эталоны для копирования:
- JSON API → `hunter/sources/remotive.py`, `hunter/sources/arbeitnow.py`
- RSS → `hunter/sources/weworkremotely.py`, `hunter/sources/solidjobs.py`

---

## 1.1 Working Nomads (`workingnomads.com`)

**Стратегия:** публичный JSON-эндпоинт листинга.

### Разведка (сделать первым шагом)
Working Nomads отдаёт вакансии через JSON. Подтвердить точный URL и форму ответа
до написания кода (DevTools → Network → XHR на странице
`https://www.workingnomads.com/jobs`). Ожидаемый кандидат:
```
GET https://www.workingnomads.com/api/jobs   (или /jobsapi)
```
Поля в каждом объекте (проверить фактические имена):
`title`, `company_name`/`company`, `url`/`slug`, `category_name`, `tags`,
`location`, `description`, `pub_date`.

> Если эндпоинт отдаёт **все** вакансии разом (как Remotive) — фильтруем категорию
> на нашей стороне (`category` содержит "Development"/"Programming"). Если есть
> server-side фильтр по category/tag — использовать его.

### Реализация — `hunter/sources/workingnomads.py`
По образцу `remotive.py`:

```python
class WorkingNomadsSource(BaseSource):
    name = "workingnomads"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "workingnomads.com" in host

    def search(self) -> list[Job]:
        # GET API_URL (+ category=development если поддерживается)
        # for raw in jobs:
        #   job = self._parse(raw)
        #   ctx = категория + tags + превью description (как в remotive _text_preview)
        #   if not self.matches_coarse_prefilter(job.title, ctx): continue
        #   dedup по job.url
        ...

    def _parse(self, raw) -> Optional[Job]:
        # title, company, url обязательны; location -> "Remote"/"<region> (Remote)"
        ...
```

Детали:
- URL вакансии: если API отдаёт `slug`, собрать
  `https://www.workingnomads.com/jobs/{slug}` (проверить реальный паттерн).
- `location`: все вакансии remote → `"Remote"` или `"<region> (Remote)"`
  (хелпер `_format_location` как в remotive.py).
- `fetch_text`: детальная страница — обычный HTML, переопределение
  необязательно (дефолтный `html_fallback` справится). Если описание целиком
  есть уже в JSON листинга — можно вернуть его напрямую в `fetch_text` без
  второго запроса (быстрее и надёжнее).
- ToS: пара запросов на цикл, User-Agent как в remotive.

### Тест — `tests/test_source_workingnomads.py`
- Зафиксировать сэмпл JSON-ответа (1–2 вакансии) в тесте, замокать
  `requests.get`.
- Проверить: `_parse` корректно тянет title/company/url; релевантный frontend
  проходит prefilter; нерелевантный (например "Customer Support") — режется;
  `matches_url` True для домена, False для чужого.

---

## 1.2 Jobspresso (`jobspresso.co`)

**Стратегия:** RSS-фид (сайт на WP Job Manager → стандартный feed).

### Разведка
Проверить доступность фида (один из):
```
https://jobspresso.co/feed/
https://jobspresso.co/?feed=job_feed
https://jobspresso.co/job-feed/
```
WP Job Manager обычно публикует `?feed=job_feed`. Подтвердить, какой отдаёт
`<item>` с вакансиями (title, link, description, возможно `<job_listing:*>` или
`<category>`). Если в одном фиде нет категорий — взять категорийный фид
(`.../jobs/software-development/feed/` или аналог).

### Реализация — `hunter/sources/jobspresso.py`
По образцу `weworkremotely.py` (RSS через `xml.etree.ElementTree`):

```python
class JobspressoSource(BaseSource):
    name = "jobspresso"

    def matches_url(self, url: str) -> bool:
        host = (urlparse(url).hostname or "").lower()
        return "jobspresso.co" in host

    def search(self) -> list[Job]:
        # GET RSS, parse_jobspresso_rss_xml(text) -> list[dict]
        # for raw: _parse -> Job; prefilter по title + category + description-preview
        ...
```

Детали:
- Заголовок `<item><title>` у WP Job Manager часто формата
  `"Job Title at Company"` или `"Company: Job Title"` — написать `_split_*`
  (как `_split_company_title` в wwr) и проверить на реальном фиде.
- `<link>` → `job.url`. `location` → `"Remote"` (Jobspresso — remote-only).
- `description` (HTML) → прогнать через `_html_to_plain` для контекста prefilter.
- Вынести `parse_jobspresso_rss_xml(xml_text)` отдельной функцией (как
  `parse_weworkremotely_rss_xml`) — удобно тестировать без сети.
- `fetch_text`: детальная — обычный HTML, дефолтный fallback ок.

### Тест — `tests/test_source_jobspresso.py`
- Зафиксировать минимальный RSS XML (2 `<item>`), вызвать
  `parse_jobspresso_rss_xml` напрямую — без сети.
- Проверить split company/title, dedup, prefilter (frontend проходит, нерелевант
  режется), `matches_url`.

---

## Общие правки для Очереди 1

### `hunter/config.py`
```python
WORKINGNOMADS_ENABLED: bool = os.getenv("WORKINGNOMADS_ENABLED", "true").lower() in ("true", "1", "yes")
JOBSPRESSO_ENABLED: bool = os.getenv("JOBSPRESSO_ENABLED", "true").lower() in ("true", "1", "yes")
```

### `hunter/sources/__init__.py`
1. Импорт тоглов в шапке (`WORKINGNOMADS_ENABLED, JOBSPRESSO_ENABLED`).
2. Блоки регистрации:
   ```python
   if WORKINGNOMADS_ENABLED:
       from hunter.sources.workingnomads import WorkingNomadsSource
       ALL_SOURCES.append(WorkingNomadsSource())

   if JOBSPRESSO_ENABLED:
       from hunter.sources.jobspresso import JobspressoSource
       ALL_SOURCES.append(JobspressoSource())
   ```
3. Добавить оба инстанса в `_fetch_roster()`.

### `CLAUDE.md`
- Таблица «Job Sources»: 2 новые строки (стратегия: JSON API / RSS).
- «Repository Layout»: упомянуть `workingnomads.py`, `jobspresso.py`.
- Источник-тоглы: дописать `WORKINGNOMADS_ENABLED`, `JOBSPRESSO_ENABLED`.
- «Scraper Health Notes»: 2 строки со статусом и датой проверки.
- «Agent Work Log»: запись о добавлении Очереди 1.
- Поправить число активных источников (17 → 19) везде, где оно встречается.

---

## Definition of Done (Очередь 1)
- [ ] Разведка обоих эндпоинтов подтверждена живым запросом.
- [ ] `workingnomads.py` + `jobspresso.py` реализованы по образцам.
- [ ] Smoke-тест каждого через `search()` даёт >0 релевантных вакансий вживую.
- [ ] Юнит-тесты на парсинг (без сети) — зелёные.
- [ ] `python -m compileall .` без ошибок.
- [ ] `pytest tests/` зелёный.
- [ ] CLAUDE.md обновлён в том же коммите.
- [ ] PR от свежего `origin/master`.
