# План: следующий источник — Remote OK (remoteok.com)

**Статус:** не реализовано.

Рецепт: [add-source.md](commands/add-source.md). Ближайшие аналоги по коду: [remotive.py](../hunter/sources/remotive.py), [arbeitnow.py](../hunter/sources/arbeitnow.py) (один JSON GET + HTML описание).

## API

- **Endpoint:** `GET https://remoteok.com/api` (в футере сайта также указаны [JSON](https://remoteok.com/json) и RSS — для hunter удобнее единый JSON-массив).
- **Формат ответа:** JSON-массив. **Первый элемент** — служебный объект `{"last_updated", "legal"}`; все последующие — вакансии. В коде: пропускать элементы, где нет поля `slug` / `position`.
- **Поля (типично):** `slug`, `id`, `position` (заголовок), `company`, `tags` (список строк), `description` (HTML), опционально `location` / регион — проверить на живом ответе; зарплата может быть в отдельных полях, если есть — маппить в `Job.salary`.
- **URL вакансии:** собрать канонический вид после проверки (обычно `https://remoteok.com/remote-jobs/{slug}` — подтвердить по ссылке из сайта).
- **Условия:** в `legal` требуется **ссылка на Remote OK** и указание источника; не злоупотреблять частотой запросов.

## Шаг 1 — Разведка (5 минут)

1. `requests.get` с `User-Agent` браузера (как у Remotive/Arbeitnow).
2. Убедиться в структуре массива и в точном URL карточки вакансии.
3. Оценить размер выдачи: при необходимости **клиентский** отбор по тегам/тексту (отдельного параметра фильтра в API может не быть).

## Шаг 2 — `hunter/sources/remoteok.py`

- `RemoteOkSource(BaseSource)`, `name = "remoteok"`.
- `search()`: один GET → отфильтровать метаданные → для каждой записи собрать `Job`, `raw` = исходный dict.
- `matches_coarse_prefilter`: в контекст передать обрезанный plain text из `description` + строка из `tags`.
- `Job.location`: если в JSON нет явного remote/города — разумный дефолт вроде `"Remote"` (как у глобальных remote-досок), чтобы проходил ваш `FILTER["locations"]` с токеном `remote`.

## Шаг 3 — `job_fetch/remoteok.py`

- Домен `remoteok.com` → [fetch_html](../job_fetch/html_fallback.py) по URL карточки (как Remotive/Arbeitnow).

## Шаг 4 — Конфиг и регистрация

- `REMOTEOK_ENABLED` в [config.py](../hunter/config.py) + строка в [.env.example](../.env.example).
- [sources/__init__.py](../hunter/sources/__init__.py), [job_fetch/__init__.py](../job_fetch/__init__.py).

## Шаг 5 — Промпты и документация

- [system_prompt.md](../prompts/system_prompt.md), [apply.md](commands/apply.md): не использовать `remoteok` как `company_name`.
- [CLAUDE.md](../CLAUDE.md): файл `remoteok.py` и toggle в списке.

## Шаг 6 — Тесты

- Unit-тест парсера на фикстуре JSON (включая случай: первый элемент только `last_updated`).
- `python -m pytest tests/ -q`.

## Шаг 7 — Ручная проверка

```bash
python -c "from hunter.sources.remoteok import RemoteOkSource; j=RemoteOkSource().search(); print(len(j)); print(j[0] if j else None)"
python -c "from job_fetch import fetch_job_text; print(fetch_job_text('https://remoteok.com/remote-jobs/<slug>')[:600])"
```

Telegram: `/hunt remoteok`

## Запасной вариант того же tier

**We Work Remotely** — общий RSS `https://weworkremotely.com/remote-jobs.rss`; реализация ближе к [solidjobs.py](../hunter/sources/solidjobs.py) (XML → `Job`), детали — `fetch_html` по `<link>`.
