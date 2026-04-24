# План: следующий источник — Remotive (remotive.com)

**Статус:** реализовано (`hunter/sources/remotive.py`, `job_fetch/remotive.py`, `REMOTIVE_ENABLED`).

Опираемся на рецепт [add-source.md](commands/add-source.md) и на образец JSON-источника [hunter/sources/nofluffjobs.py](../hunter/sources/nofluffjobs.py) / [hunter/sources/arbeitnow.py](../hunter/sources/arbeitnow.py).

## Зачем Remotive

- Публичный **JSON API** без ключа: `GET https://remotive.com/api/remote-jobs` (опционально `?category=...`, `search=...`, `limit=...`).
- Документация и условия: [remotive.com/remote-jobs/api](https://remotive.com/remote-jobs/api), репозиторий `remotive-com/remote-jobs-api`.
- Важно: **атрибуция** (ссылка на Remotive и на оригинал вакансии), **не злоупотреблять частотой** запросов (для hunter достаточно **одного** запроса за цикл с нужными query-параметрами), данные могут отставать от сайта.

## Шаг 1 — Проверка API (перед кодом)

1. Один запрос с заголовком `User-Agent` как у браузера (как в [arbeitnow.py](../hunter/sources/arbeitnow.py)), при необходимости `Accept: application/json`.
2. Зафиксировать структуру: корневой ключ `jobs` (массив), поля для `Job`: как минимум `title`, `company_name` или `company`, `url`, локация/remote (как приходит в JSON — маппинг в `Job.location`).
3. Решить, нужен ли **один** вызов со `search=` под ваш стек (frontend/angular/typescript) или несколько категорий — без лишних round-trip.

## Шаг 2 — `hunter/sources/remotive.py`

- Класс `RemotiveSource(BaseSource)`, `name = "remotive"`.
- `search()`: `requests.get(API, params=..., headers=HEADERS, timeout=...)`, разбор JSON → список `Job`.
- Сохранять исходный объект в `Job.raw` для фильтров ([filters.py](../hunter/filters.py): немецкий, react/angular и т.д.).
- `matches_coarse_prefilter(title, context)`: в `context` передать строку из тегов/описания из API, если поля короткие.
- Ошибки сети — лог и `[]`.

## Шаг 3 — `job_fetch/remotive.py`

- Если страница вакансии стабильно отдаёт HTML — обёртка над [html_fallback.fetch_html](../job_fetch/html_fallback.py) (как [arbeitnow.py](../job_fetch/arbeitnow.py)).
- Если в `raw` уже есть полное описание и URL ведёт на внешний ATS — оставить `fetch_html` по `job.url` (как есть).

## Шаг 4 — `hunter/config.py`

- Блок `REMOTIVE_ENABLED` с `os.getenv("REMOTIVE_ENABLED", "true")` по аналогии с `ARBEITNOW_ENABLED`.

## Шаг 5 — `hunter/sources/__init__.py`

- Условный импорт и `ALL_SOURCES.append(RemotiveSource())`.

## Шаг 6 — `job_fetch/__init__.py`

- Ветка `if "remotive.com" in domain:` → `fetch_remotive(url)` до общего `fetch_html`.

## Шаг 7 — Сопутствующие правки (как для Arbeitnow)

- [prompts/system_prompt.md](../prompts/system_prompt.md) и [.claude/commands/apply.md](commands/apply.md): не использовать `remotive` как `company_name`.
- [CLAUDE.md](../CLAUDE.md): строка в дереве `sources/` и в списке toggles `REMOTIVE_ENABLED`.
- [.env.example](../.env.example): `REMOTIVE_ENABLED=true`.

## Шаг 8 — Тесты

- Минимум: unit-тест парсера `_parse` / нормализации URL из фикстуры JSON (без сети), либо smoke в CI с моком `requests.get`.
- Прогон: `python -m pytest tests/ -q`.

## Шаг 9 — Ручная проверка

```bash
python -c "from hunter.sources.remotive import RemotiveSource; j=RemotiveSource().search(); print(len(j)); print(j[0] if j else None)"
python -c "from job_fetch import fetch_job_text; print(fetch_job_text('https://remotive.com/remote-jobs/...')[:800])"
```

## Альтернатива тому же приоритету

Если Remotive временно режет IP **403**, следующий кандидат из того же tier A: **Remote OK** (`remoteok.com/json`) или **We Work Remotely** (RSS, по аналогии с [solidjobs.py](../hunter/sources/solidjobs.py)).
