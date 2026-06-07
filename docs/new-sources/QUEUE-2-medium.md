# Очередь 2 — средний effort (HTML-скрейп, возможен Cloudflare)

Брать **после** Очереди 1, по одному источнику = по одному PR. Порядок —
по убыванию выхлопа: Built In → JustRemote → Remote.co.

Эталоны:
- `__NEXT_DATA__` / dehydratedState + cloudscraper → `hunter/sources/pracuj.py`,
  `hunter/sources/theprotocol.py`
- BeautifulSoup DOM → `hunter/sources/bulldogjob.py`,
  `hunter/sources/remoteleaf.py`
- JSON-LD в детальной странице → `hunter/sources/theprotocol.py` `fetch_text`

> Перед кодом по каждому: открыть листинг в DevTools, определить стратегию
> (JSON-эндпоинт XHR? `__NEXT_DATA__`? чистый DOM?). JSON/NEXT_DATA всегда
> предпочтительнее парсинга DOM — стабильнее.

---

## 2.1 Built In (`builtin.com`) — приоритет, максимум frontend-объёма

**Почему:** большой объём US/remote tech-ролей, много React/Angular. Кандидат
готов на не-EU → география не минус.

### Разведка
- Листинг: `https://builtin.com/jobs` с фильтрами в query
  (`?search=angular`, `?search=frontend`, категория Dev+Engineering, remote-флаг).
- Проверить: есть ли XHR JSON за листингом (Built In — Next.js/React, вероятен
  внутренний API `/api/...` или `__NEXT_DATA__`). Если да — использовать его.
- Иначе — BeautifulSoup по карточкам листинга.
- Возможна пагинация (`?page=N`) — ограничить 1–2 страницами на запрос
  (как LinkedIn: 2 страницы).

### Реализация — `hunter/sources/builtin.py`
```python
class BuiltInSource(BaseSource):
    name = "builtin"

    def matches_url(self, url):
        return "builtin.com" in (urlparse(url).hostname or "").lower()

    def search(self) -> list[Job]:
        # для каждого LISTING_URL (frontend / angular / react, remote):
        #   fetch -> извлечь карточки (JSON или DOM)
        #   _parse -> Job (title, company, url абсолютный, location)
        #   prefilter, dedup по url
        ...

    def fetch_text(self, url):
        # детальная страница Built In: JSON-LD JobPosting если есть, иначе DOM
        ...
```
Детали:
- LISTING_URLS: 2–3 запроса (frontend / angular / react), с remote-фильтром если
  поддерживается.
- URL карточки часто относительный → склеить с `https://builtin.com`.
- `location`: брать как есть; для remote-вакансий ставить `"Remote"` хвостом.
- Built In может ставить мягкую защиту — начать с `requests` + браузерный
  User-Agent; если ловим 403 — переключить на `cloudscraper`.

### Тест — `tests/test_source_builtin.py`
- Зафиксировать сэмпл (JSON-фрагмент ИЛИ кусок HTML листинга), замокать сеть.
- Проверить парсинг карточки, абсолютизацию URL, prefilter, `matches_url`.

---

## 2.2 JustRemote (`justremote.co`)

**Почему:** remote-dev роли, remote-first компании. Объём средний.

### Разведка
- Листинг: `https://justremote.co/remote-developer-jobs` (и
  `/remote-front-end-jobs` если есть).
- ⚠️ Cloudflare вероятен → сразу планировать `cloudscraper` (уже в зависимостях,
  см. pracuj.py).
- Часть функциональности (Power Search) за paywall — нам нужен только публичный
  листинг. Проверить, что бесплатные карточки отдают title/company/url без
  логина.
- Определить стратегию: `__NEXT_DATA__` (сайт на Next?) или DOM.

### Реализация — `hunter/sources/justremote.py`
По образцу `pracuj.py` (cloudscraper + извлечение структуры):
```python
import cloudscraper
_scraper = cloudscraper.create_scraper()

class JustRemoteSource(BaseSource):
    name = "justremote"
    def matches_url(self, url):
        return "justremote.co" in (urlparse(url).hostname or "").lower()
    def search(self) -> list[Job]: ...
    def fetch_text(self, url): ...  # cloudscraper + JSON-LD/DOM, html_fallback на провал
```
Детали:
- На 403/429 — не каскадить: один retry с бэкоффом, затем `[]` (как pracuj).
- При желании переиспользовать `hunter/rate_limiter.py` `DomainLimiter`, но для
  одного листингового запроса на цикл это избыточно — достаточно cloudscraper.
- `location` → `"Remote"`.

### Тест — `tests/test_source_justremote.py`
- Сэмпл листинга (зафиксированный фрагмент), мок `cloudscraper`/`requests`.
- Парсинг, dedup, prefilter, `matches_url`. Поведение на 403 (возврат `[]`).

---

## 2.3 Remote.co (`remote.co`)

**Почему:** курируемые remote-вакансии, чистые данные. Объём небольшой, частично
дублирует WWR/Remotive — поэтому ставим последним в очереди.

### Разведка
- Листинг: `https://remote.co/remote-jobs/developer/` (категория developer).
- Проверить RSS/feed (раньше был) — если есть, делать как RSS (проще, → ближе к
  Очереди 1 по сложности). Иначе DOM-парсинг карточек.
- Проверить пагинацию.

### Реализация — `hunter/sources/remoteco.py`
- Если есть фид → копировать структуру `weworkremotely.py`.
- Если только HTML → `bulldogjob.py`/`remoteleaf.py` как образец DOM.
```python
class RemoteCoSource(BaseSource):
    name = "remoteco"
    def matches_url(self, url):
        return "remote.co" in (urlparse(url).hostname or "").lower()
```
Детали: `location="Remote"`; URL карточек абсолютизировать; prefilter по
title + описание-превью.

### Тест — `tests/test_source_remoteco.py`
Зафиксированный сэмпл (RSS или HTML), мок сети, парсинг + dedup + prefilter +
`matches_url`.

---

## Общие правки (на КАЖДЫЙ источник Очереди 2)

### `hunter/config.py`
```python
BUILTIN_ENABLED:   bool = os.getenv("BUILTIN_ENABLED",   "true").lower() in ("true", "1", "yes")
JUSTREMOTE_ENABLED: bool = os.getenv("JUSTREMOTE_ENABLED", "true").lower() in ("true", "1", "yes")
REMOTECO_ENABLED:  bool = os.getenv("REMOTECO_ENABLED",  "true").lower() in ("true", "1", "yes")
```

### `hunter/sources/__init__.py`
Импорт тогла + блок `if ..._ENABLED: ALL_SOURCES.append(...)` + инстанс в
`_fetch_roster()`.

### `CLAUDE.md`
Строка в «Job Sources», запись в «Repository Layout», тогл в списке, строка в
«Scraper Health Notes» (для Cloudflare-источников отметить PARTIAL при риске),
запись в «Agent Work Log». Обновить счётчик активных источников.

---

## Definition of Done (на каждый источник)
- [ ] Разведка стратегии подтверждена живым запросом (JSON/NEXT_DATA/DOM/RSS).
- [ ] Источник реализован по подходящему образцу.
- [ ] `search()` вживую даёт >0 релевантных; `fetch_text()` на реальном URL
      возвращает осмысленный текст.
- [ ] Cloudflare-источники: проверено поведение на 403/429 (graceful `[]`).
- [ ] Юнит-тесты (без сети) зелёные; `pytest tests/` зелёный;
      `python -m compileall .` чисто.
- [ ] CLAUDE.md обновлён в том же коммите.
- [ ] Отдельный PR от свежего `origin/master`.

## После Очереди 2 — проверка дублей
С ростом числа глобальных досок замерить долю дублей в Telegram. Если заметна —
вернуться к усилению `dedup_key` (см. OVERVIEW «Дедуп и шум»).
