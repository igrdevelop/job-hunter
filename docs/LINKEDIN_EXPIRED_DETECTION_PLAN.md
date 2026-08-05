# LinkedIn Expired Detection via Playwright Session

## Проблема

Без авторизации LinkedIn не отдаёт текст «No longer accepting applications». Бот получает полное описание вакансии → `expired_check` не срабатывает → pipeline тратит 13 LLM-вызовов (~$0.31) на мёртвую вакансию.

## Решение

В apply pipeline, при `fetch_job_text` для LinkedIn-URL, использовать **Playwright с `LINKEDIN_STORAGE_STATE`** (та же сессия, что и для поиска). Авторизованная страница содержит маркер expired → существующий `expired_check` ловит его → pipeline останавливается за $0.

## Что НЕ трогаем

- `/check_expired` — периодическая проверка unsent-строк. Работает без сессии, гоняет десятки URL. Не трогаем — лишняя нагрузка на сессию без финансовой выгоды.
- Gmail enrichment — LinkedIn URLs и так в `GMAIL_ENRICH_SKIP_HOSTS`.
- LinkedIn search scraper — уже использует сессию, не меняется.

## Поток данных (до и после)

```
apply_agent.py → fetch_job_text(url)
                     │
                     ├─ linkedin.com?
                     │    │
                     │    ├─ СЕЙЧАС: requests.get (без сессии)
                     │    │  → HTML без «No longer accepting»
                     │    │  → expired_check = False
                     │    │  → LLM → $0.31 впустую
                     │    │
                     │    └─ ПОСЛЕ: Playwright + storage_state
                     │       → HTML с «No longer accepting»
                     │       → expired_check = True
                     │       → SKIP, $0.00
                     │
                     └─ другие домены → без изменений
```

## Шаги реализации

### Шаг 1 — Новый fetcher в `hunter/sources/linkedin.py`

Добавить метод `fetch_text_with_session(url) → str` в `LinkedInSource`:

- Если `LINKEDIN_STORAGE_STATE` задан и файл существует — запуск Playwright (headed=False), загрузка страницы, извлечение `innerText`
- Если сессии нет — fallback на текущий `fetch_text(url)` (requests без сессии, поведение не меняется)
- Таймаут: 15 секунд (как у обычного fetch)
- Ошибка Playwright → fallback на текущий метод (best-effort, не ломаем pipeline)

```python
def fetch_text_with_session(self, url: str) -> str:
    storage = config.LINKEDIN_STORAGE_STATE
    if not storage or not Path(storage).exists():
        return self.fetch_text(url)
    try:
        return self._playwright_fetch(url, storage)
    except Exception as e:
        logger.warning("[linkedin] Session fetch failed: %s, falling back", e)
        return self.fetch_text(url)
```

### Шаг 2 — Playwright fetch helper

Приватный метод `_playwright_fetch(url, storage_state) → str`:

- Переиспользовать паттерн из `hunter/sources/inhire.py` (уже делает Playwright-fetch в этом проекте)
- `sync_playwright` (не async — `fetch_job_text` вызывается из subprocess `apply_agent.py`, не из asyncio event loop бота)
- Запуск chromium, `context(storage_state=...)`, `page.goto(url, wait_until="domcontentloaded")`, `page.inner_text("body")`
- Закрываем browser в `finally`
- Блокировать загрузку картинок/шрифтов (как в `linkedin_scout/browser.py` — экономия памяти)

### Шаг 3 — Диспетчер `hunter/sources/__init__.py`

Функция `fetch_job_text(url)` сейчас вызывает `source.fetch_text(url)` для подходящего источника. Для LinkedIn нужно вызывать `fetch_text_with_session(url)` **только из apply pipeline**.

Добавить параметр `use_session`:

```python
# hunter/sources/__init__.py
def fetch_job_text(url: str, *, use_session: bool = False) -> str:
    for source in ALL_SOURCES:
        if source.matches_url(url):
            if use_session and hasattr(source, 'fetch_text_with_session'):
                return source.fetch_text_with_session(url)
            return source.fetch_text(url)
    return html_fallback_fetch(url)
```

### Шаг 4 — Подключить в apply pipeline

В `hunter/apply_api.py` (Step 1 — fetch job text) изменить вызов:

```python
# Было:
text = fetch_job_text(url)

# Стало:
text = fetch_job_text(url, use_session=True)
```

Аналогично в `hunter/apply_cli.py`, если там свой вызов `fetch_job_text`.

**НЕ трогать** вызовы в: `expired_marker.py`, `gmail_enricher.py`, `repost_gate.py`.

### Шаг 5 — Тесты

- Unit-тест `fetch_text_with_session`: мокнуть Playwright, проверить что при наличии storage_state вызывается `_playwright_fetch`
- Unit-тест fallback: без storage_state / при ошибке Playwright → обычный `fetch_text`
- Тест диспетчера: `fetch_job_text(linkedin_url, use_session=True)` → вызывает `fetch_text_with_session`
- Тест диспетчера: `fetch_job_text(linkedin_url)` без `use_session` → обычный `fetch_text` (обратная совместимость)
- `pytest tests/ -x`, `ruff check . && ruff format .`

## Файлы для изменения

| Файл | Изменение |
|------|-----------|
| `hunter/sources/linkedin.py` | + `fetch_text_with_session()`, `_playwright_fetch()` |
| `hunter/sources/__init__.py` | + параметр `use_session` в `fetch_job_text()` |
| `hunter/apply_api.py` | `fetch_job_text(url, use_session=True)` |
| `hunter/apply_cli.py` | то же, если есть свой вызов fetch |
| `CLAUDE.md` | документация |

## Нагрузка на сессию

- Apply pipeline запускается только для вакансий, прошедших фильтры и деdup
- LinkedIn-вакансий среди них: **~5–15 в день**
- Каждая — 1 Playwright page load (domcontentloaded, без картинок/шрифтов)
- Поверх ~150 поисковых запросов/день — прирост **<10%**

## Верификация

1. Найти expired LinkedIn-вакансию (или дождаться следующей)
2. Запустить: `python -c "from hunter.sources.linkedin import LinkedInSource; s = LinkedInSource(); print(s.fetch_text_with_session('https://linkedin.com/jobs/view/...'))"` 
3. Убедиться что текст содержит «No longer accepting applications»
4. Запустить apply: вставить URL в Telegram → бот должен ответить «EXPIRED» без LLM-вызовов

## Заметки

- Fallback гарантирует обратную совместимость: без `LINKEDIN_STORAGE_STATE` или при ошибке Playwright — поведение не меняется
- Риск: LinkedIn может показывать cookie wall даже с сессией (протухший токен) — тогда fallback сработает автоматически
