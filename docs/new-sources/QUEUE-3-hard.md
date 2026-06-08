# Очередь 3 — высокий объём, тяжёлый парсинг

> **СТАТУС (2026-06-08): обе ОТЛОЖЕНЫ после разведки.** Код не писался.
> - **Wellfound ⛔** — жёсткий HTTP **403** на любой запрос (1692-байт challenge),
>   непробиваем ни `requests`, ни `cloudscraper`. Нужен Playwright + сессия логина
>   (в Docker заблокирован, см. блокер ниже). Реализует план-вариант C (отказ).
> - **Jobgether ⛔** — доступен (Cloudflare пропускает), но **нет чистого JSON-листинга**
>   (`/feed/remote-jobs.json` = сводка на 82 байта; Algolia-креды не извлекаются;
>   данные только в detail-страницах JSON-LD). Листинг — хрупкий Tailwind-DOM без
>   стабильных `data-id`/`testid`. Серверного frontend-фильтра нет (`?search=`
>   игнорируется сервером). В dev-категории (50 вакансий) — **0 попаданий** под наш
>   title-фильтр (менеджеры/архитекторы/дженерик «Software Engineer»). Низкий ROI,
>   плохой фит — не оправдывает хрупкий парсер.
>
> Соответствует исходной оценке плана: «низкий ROI — не начинать, пока нет явной
> потребности в объёме». Вернуться при необходимости (Wellfound — только с
> Playwright+login; Jobgether — если изменится структура/появится API).

Браться **только если** Очереди 1–2 дают недостаточный поток. Высокий риск,
высокая стоимость поддержки (бот-защита / SPA). Каждый — отдельный PR.

> Принцип: всегда сначала искать backend JSON-эндпоинт (DevTools → Network → XHR).
> Реальный API на порядок стабильнее парсинга SPA-DOM или обхода бот-защиты.
> Headless-браузер (Playwright) — последнее средство.

---

## 3.1 Wellfound (`wellfound.com`, ex-AngelList)

**Почему хотим:** ~130K вакансий, стартапы, много remote/frontend.
**Почему тяжело:** GraphQL за авторизацией + сильная бот-защита (Cloudflare/PerimeterX),
сильный анти-скрейпинг. Часть листинга требует залогиненного аккаунта.

### Разведка (критична — от неё зависит, реализуемо ли вообще)
1. DevTools → Network на `https://wellfound.com/jobs` → найти GraphQL POST
   (`/graphql`). Зафиксировать: query name, переменные (фильтры по role/remote),
   обязательные заголовки (`apollographql-client-*`, CSRF, cookies).
2. Проверить, отдаёт ли GraphQL данные **без** залогиненной сессии. Если требует
   куки авторизации — оценить, готовы ли хранить сессию (как LinkedIn login в
   `tools/`).
3. Оценить бот-защиту: проходит ли `cloudscraper`, или нужен полноценный
   браузер с stealth.

### Возможные стратегии (в порядке предпочтения)
- **A. Прямой GraphQL** через `requests`/`cloudscraper` с воспроизведёнными
  заголовками — если работает без логина. Лучший вариант.
- **B. Playwright** с реальной сессией (логин через `tools/wellfound_login.py`,
  по образцу LinkedIn-логина) — если GraphQL закрыт. Требует Playwright в Docker
  (см. блокер ниже).
- **C. Отказ** — если защита непробиваема разумными силами. Зафиксировать вывод в
  Health Notes и не тратить время.

### Реализация — `hunter/sources/wellfound.py`
```python
class WellfoundSource(BaseSource):
    name = "wellfound"
    def matches_url(self, url):
        host = (urlparse(url).hostname or "").lower()
        return "wellfound.com" in host or "angel.co" in host
    def search(self) -> list[Job]: ...   # GraphQL или Playwright
    def fetch_text(self, url): ...
```
Детали:
- Учесть старый домен `angel.co` в `matches_url`.
- Жёсткие таймауты и graceful `[]` при блокировке — не валить хант.
- Тогл по умолчанию **`false`** (как INHIRE), пока не подтверждена стабильность.

### Тест — `tests/test_source_wellfound.py`
Зафиксировать сэмпл GraphQL-ответа (JSON), мок транспорта; парсинг узлов в `Job`,
`matches_url` для обоих доменов.

---

## 3.2 Jobgether (`jobgether.com`)

**Почему хотим:** 100K+ remote, AI-matching, растущая база.
**Почему тяжело:** SPA с AI-матчингом, контент рендерится клиентом.

### Разведка
1. DevTools → Network → XHR на `https://jobgether.com/`(поиск/листинг) → найти
   JSON API за поиском (часто у таких платформ есть открытый `/api/...` или
   Algolia/Elastic-эндпоинт). **Если найдётся — это переводит источник почти в
   Очередь 1** (чистый JSON), Playwright не нужен.
2. Если открытого API нет → Playwright (см. блокер).

### Реализация — `hunter/sources/jobgether.py`
- Если есть JSON API → копировать `remotive.py`/`arbeitnow.py`.
- Если только SPA → копировать `inhire.py` (Playwright, `asyncio.run` из `search`).
```python
class JobgetherSource(BaseSource):
    name = "jobgether"
    def matches_url(self, url):
        return "jobgether.com" in (urlparse(url).hostname or "").lower()
```
Тогл по умолчанию **`false`** если решение на Playwright (как INHIRE).

### Тест — `tests/test_source_jobgether.py`
Сэмпл JSON-ответа API (или замоканный результат `page.evaluate`), парсинг в `Job`,
`matches_url`.

---

## БЛОКЕР: Playwright в Docker

Актуально для 3.1-вариант-B и 3.2-если-SPA. См. CLAUDE.md «Known Issues #4»:

> Playwright не установлен в Docker → Inhire всегда возвращает `[]`.
> Включение: раскомментировать `playwright` в `requirements.txt` +
> `RUN playwright install chromium --with-deps` в `Dockerfile` (+~500 МБ к образу).

**Решение принять ДО кодинга источника на Playwright:**
- Вариант 1: установить Playwright в образ (тогда заодно оживёт Inhire) — но +500 МБ
  и дольше билд.
- Вариант 2: найти JSON-эндпоинт и обойтись без браузера (предпочтительно).
- Вариант 3: гонять Playwright-источники только локально (тогл `false` в Docker).

Если выбран Playwright в образе — это отдельная подзадача/PR (инфраструктура),
до источников Очереди 3.

---

## Общие правки (на каждый источник)
- `hunter/config.py`: `WELLFOUND_ENABLED` / `JOBGETHER_ENABLED`
  (по умолчанию `false`, пока не доказана стабильность).
- `hunter/sources/__init__.py`: импорт тогла + блок регистрации + инстанс в
  `_fetch_roster()`.
- `CLAUDE.md`: «Job Sources», «Repository Layout», тоглы, «Scraper Health Notes»
  (честно отметить риск/PARTIAL/локально-только), «Agent Work Log».

---

## Definition of Done (на каждый источник)
- [ ] Разведка: найден стабильный путь (GraphQL/JSON) ИЛИ принято осознанное
      решение про Playwright/отказ — зафиксировано в Health Notes.
- [ ] Если Playwright — вопрос с Docker-образом решён отдельно ДО источника.
- [ ] Источник реализован, тогл по умолчанию `false`.
- [ ] `search()` вживую даёт релевантные вакансии (хотя бы локально).
- [ ] Юнит-тесты (без сети) зелёные; `pytest tests/` зелёный;
      `python -m compileall .` чисто.
- [ ] CLAUDE.md обновлён в том же коммите.
- [ ] Отдельный PR от свежего `origin/master`.

---

## Honest take
Wellfound и Jobgether — это «добивка» на случай, если глобального потока с
Очередей 1–2 (Working Nomads, Jobspresso, Built In, JustRemote, Remote.co) +
существующих RemoteOK/WWR/Remotive окажется мало. С учётом стоимости поддержки
бот-защиты и SPA, ROI здесь самый низкий — не начинать, пока нет явной
потребности в объёме.
