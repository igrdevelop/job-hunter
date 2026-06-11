# Fix Plan — pracuj.pl 429 «Too Many Requests» во время `/hunt gmail`

**Branch:** `fix/pracuj-rate-limit` (от `origin/master`) → PR в `master`
**Дата:** 2026-06-04
**Триггер:** прогон `/hunt gmail` — 4 новых pracuj-вакансии, все упали с `429`, затем
ретрай-цикл добил тот же хост ещё 6× `429`.

## Статус: РЕАЛИЗОВАНО (2026-06-05)

Шаги 1–5 + 7 выполнены, каждый — отдельный коммит с тестами. Шаг 6 исключён
(ушёл в `fix/expired-vacancies`). Итог: **1142 теста зелёные** (+26 новых).

| Шаг | Коммит | Тесты |
|-----|--------|-------|
| 1. Вынос DomainLimiter → rate_limiter.py | `483535a` | +7 |
| 2. Per-host троттлинг в gmail_enricher | `3d7a7a4` | +2 |
| 3. Backoff на 429 в pracuj | `4b89ee7` | +4 |
| 4. Circuit breaker в _retry_failed | `cdabeb7` | +2 |
| 5. rate_limited без эскалации | `bd43c07` | +6 |
| 7. Заголовок из URL-слага | `ad2c86b` | +5 |

---

## Решения (зафиксированы с владельцем)

- **Scope этого PR:** шаги **1–5 + 7**. Шаг **6 (skip истёкших) исключён** — уходит в
  отдельную ветку `fix/expired-vacancies`, здесь expired-логику не трогаем.
- **Троттлинг pracuj по умолчанию:** умеренный — **2 одновременных запроса / 1.0 c пауза**
  (как `EXPIRED_CHECK_DOMAIN_LIMIT=2`, `EXPIRED_CHECK_DOMAIN_DELAY=1.0`). Значения в `.env`.
- **`enrich_jobs` запускается в worker-потоке** (`asyncio.to_thread(source.search)` в
  [`main.py:116`](hunter/main.py)), не в event loop — переход на async через `asyncio.run`
  внутри безопасен; альтернатива — threads + `threading.Semaphore`. Решается на этапе кода.
- **Git-workflow:** ветка от `origin/master`, PR в `master`.

---

## 1. Симптом (из лога бота)

```
[1/4] [pracuj] Angular Developer ...  → ❌ 429 Too Many Requests
[2/4] [pracuj] Delphi Developer ...   → ❌ 429
[3/4] [pracuj] Middle Projektant ...  → ❌ 429
🛑 3 consecutive failures — stopping batch. Skipped 1.
🔄 Retrying 6 previously failed jobs...
[1/6..6/6] [pracuj] ...               → ❌ 429 ×6
```

`AUTO_APPLY=true`, провайдер `claude-sonnet-4`. Хант — только `gmail`.

---

## 2. Корневая причина

`429` — это **не** поломка парсера, а **anti-bot rate limiting (Cloudflare) со стороны
pracuj.pl**. Бот шлёт пачку запросов к одному хосту без троттлинга.

### 2.1. Главный источник — параллельное gmail-обогащение

[`hunter/gmail_enricher.py:139`](hunter/gmail_enricher.py) — `enrich_jobs()` запускает
`ThreadPoolExecutor(max_workers=GMAIL_ENRICH_CONCURRENCY)` (по умолчанию **5**) и шлёт до
5 запросов **одновременно, без задержек и без учёта домена**. Когда gmail-дайджест pracuj
содержит много pracuj-ссылок, 5 запросов бьют в `pracuj.pl` разом → Cloudflare выдаёт `429`.
Хост попадает в rate-limit ещё **до** фазы apply.

```python
# gmail_enricher.py — как сейчас (нет per-host лимита, нет паузы)
with ThreadPoolExecutor(max_workers=GMAIL_ENRICH_CONCURRENCY) as pool:
    future_to_url = {pool.submit(_enrich_one, job): job.url for job in jobs}
```

### 2.2. Apply-фаза добивает уже rate-limited хост

`_auto_apply_all` ([`hunter/main.py:335`](hunter/main.py)) идёт последовательно с
`APPLY_DELAY_SEC`, но:
- внутри одного `fetch_text` ([`hunter/sources/pracuj.py:314`](hunter/sources/pracuj.py))
  идут **три** попытки подряд по одному URL (cloudscraper → requests → `html_fallback`),
  без backoff на `429` — то есть один «мёртвый» URL = до 3 ударов;
- лимит для pracuj общий с другими хостами — нет отдельного бюджета.

### 2.3. Ретрай-цикл без circuit breaker

`_retry_failed` ([`hunter/main.py:394`](hunter/main.py)) прогоняет **все** ранее упавшие
pracuj-вакансии (6 шт.) **без** проверки `consecutive_fails`, которая есть в основном
батче ([`hunter/main.py:371`](hunter/main.py)). Итог — ещё 6× `429` по уже забаненному хосту.

### 2.4. Временный `429` превращается в перманентный отказ

В ретрае каждый фейл вызывает `increment_fail_count`
([`hunter/main.py:430`](hunter/main.py)); по достижении `MAX_FAIL_RETRIES` — «🚫 Giving up».
То есть временный rate-limit накручивает счётчик и ведёт к необратимому отказу от валидной
вакансии.

### 2.5. (Смежно) Истёкшие вакансии вообще не надо тянуть

В заголовках стоит `pracodawca zakończył rekrutację` / `zakończył zbieranie zgłoszeń` —
оффер закрыт. Детектор архива в pracuj (`_ARCHIVED_PATTERNS`) срабатывает только **после**
успешного скачивания HTML, которого при `429` нет. Такие можно отсеивать по заголовку заранее.

### 2.6. (Смежно) Заголовок ≠ URL в gmail-парсере

«Angular Developer (K/M/N)» → URL `...react-next-js-w-dziale-produktu...`;
«Fullstack (TypeScript + Angular)» → URL `...junior-frontend-developer...`.
Пары заголовок↔ссылка в дайджест-парсере перепутаны — отдельный баг.

---

## 3. Ключевой принцип решения

**Лимитировать надо per-host, а не глобально.** `GMAIL_ENRICH_CONCURRENCY=5` — общий
потолок: если все 5 ссылок ведут на pracuj, всё равно 5 одновременных ударов. Cloudflare
считает по `IP + домен`, поэтому ограничение должно быть на уровне домена.

**Готовый паттерн уже есть в проекте** и проверен на проде в `/check_expired`:
`_DomainLimiter` ([`hunter/expired_marker.py:141`](hunter/expired_marker.py)) — global
semaphore + **per-domain semaphore** + **per-domain delay**. Конфиг:
`EXPIRED_CHECK_DOMAIN_LIMIT=2`, `EXPIRED_CHECK_DOMAIN_DELAY=1.0`
([`hunter/config.py:332`](hunter/config.py)). Решение — переиспользовать этот код, а не
писать новый.

---

## 4. Пошаговый план

### Шаг 0 — Подготовка (этот worktree)
- [x] Создать worktree `fix/pracuj-rate-limit` от `origin/master`.
- [x] Зафиксировать анализ в этом документе.
- [ ] Прогнать `pytest tests/` для базовой линии (зелёный старт).

### Шаг 1 — Вынести `_DomainLimiter` в общий модуль  *(рефактор, LOW risk)*
- [ ] Создать `hunter/rate_limiter.py`, перенести туда `_DomainLimiter` и хелпер `_domain`
      из `expired_marker.py` без изменения поведения.
- [ ] В `expired_marker.py` импортировать из нового модуля (поведение `/check_expired`
      не меняется).
- [ ] Тест: `_DomainLimiter` ограничивает ≤ N одновременных на домен и выдерживает delay.
- [ ] `python -m compileall hunter` + `pytest tests/test_expired*.py`.

### Шаг 2 — Подключить per-host троттлинг в gmail-обогащении  *(основной фикс, MEDIUM risk)*
- [ ] Добавить конфиг в [`hunter/config.py`](hunter/config.py):
      - `GMAIL_ENRICH_DOMAIN_LIMIT` (default `2`)
      - `GMAIL_ENRICH_DOMAIN_DELAY` (default `1.0`)
      - точечно для pracuj: `PRACUJ_HOST_CONCURRENCY` (default `1`),
        `PRACUJ_HOST_DELAY_SEC` (default `2.5`)
- [ ] Переписать `enrich_jobs()` ([`hunter/gmail_enricher.py:131`](hunter/gmail_enricher.py))
      так, чтобы фетчи шли через `_DomainLimiter`:
      - вариант (а) перевести на `asyncio` + `asyncio.run` внутри `enrich_jobs`
        (limiter уже async) — чище, совпадает с expired_marker;
      - вариант (б) оставить `ThreadPoolExecutor`, но добавить per-host
        `threading.Semaphore` + задержку (если переход на async задевает синхронных
        вызывающих). **Решить после проверки вызывающих `enrich_jobs`.**
      - pracuj.pl получает лимит 1 + пауза ~2.5 c; остальные хосты — как сейчас.
- [ ] Тест: при N pracuj-ссылках одновременных запросов к pracuj ≤ 1; justjoin/linkedin
      по-прежнему параллельны.

### Шаг 3 — Backoff на `429` в pracuj-фетчере  *(LOW risk)*
- [ ] В `_fetch_detail_html` ([`hunter/sources/pracuj.py:292`](hunter/sources/pracuj.py))
      при ответе `429`: **не** перебирать остальные стратегии немедленно; уважать
      `Retry-After`, иначе экспоненциальная пауза (напр. 2 → 4 c), максимум 1–2 ретрая,
      затем пробросить, чтобы вызвавший пометил FAIL.
- [ ] Тест: `429` → один backoff-sleep, без каскада из 3 фолбэков по одному URL.

### Шаг 4 — Circuit breaker в `_retry_failed`  *(LOW risk)*
- [ ] Добавить `consecutive_fails` со `break` при `>= 3`
      в [`hunter/main.py:394`](hunter/main.py), как в `_auto_apply_all`.
- [ ] Тест: 3 подряд FAIL в ретрае → цикл останавливается, остальные не трогаются.

### Шаг 5 — Не штрафовать за временный `429`  *(LOW risk)*
- [ ] Различать тип фейла: апплай-функции должны возвращать причину
      (`rate_limited` / `expired` / `parse_error`).
- [ ] `increment_fail_count` вызывать **только** для неустранимых ошибок; `rate_limited`
      — не инкрементить (повторим в следующем ханте без приближения к «Giving up»).
- [ ] Тест: серия `429` не доводит до `MAX_FAIL_RETRIES`.

### Шаг 6 — Ранний skip истёкших вакансий  *(ИСКЛЮЧЁН из этого PR)*
- [ ] **Не делаем здесь.** Уходит в ветку `fix/expired-vacancies`, чтобы не пересекаться.

### Шаг 7 — Заголовок↔URL в gmail-парсере  *(ВКЛЮЧЕНО в этот PR)*
- [ ] Воспроизвести на дайджест-фикстуре pracuj в `gmail_parsers.py`, починить пары.
- [ ] Тест: каждому заголовку соответствует его собственный URL.

### Шаг 8 — Финализация
- [ ] `python -m compileall .` + полный `pytest tests/`.
- [ ] Обновить таблицу источников / Agent Work Log в `CLAUDE.md`.
- [ ] Коммит на `fix/pracuj-rate-limit`, PR в `master`.

---

## 5. Объём по риску

| Шаг | Риск | Эффект |
|-----|------|--------|
| 1. Вынести `_DomainLimiter` | LOW (чистый рефактор) | переиспользуемый троттл |
| 2. Per-host в gmail_enricher | **MEDIUM** | **снимает корневую причину `429`** |
| 3. Backoff на 429 в pracuj | LOW | меньше ударов на URL |
| 4. Circuit breaker в retry | LOW | retry не добивает хост |
| 5. Не штрафовать за 429 | LOW | валидные вакансии не теряются |
| 6. Skip expired | LOW | не тратим попытки на мёртвые |
| 7. Заголовок↔URL | отдельный PR | корректный таргетинг CV |

**Минимально достаточно для устранения симптома из лога: шаги 1–4.**
Шаги 5–6 — устойчивость, шаг 7 — отдельный баг.

---

## 6. Открытые вопросы перед кодом

1. **async vs threads** в `enrich_jobs` (шаг 2): зависит от вызывающих `enrich_jobs` —
   проверить, не из async-контекста ли он зовётся (тогда `asyncio.run` нельзя).
2. **Значения по умолчанию** для pracuj (`1` / `2.5 c`) — стартовые; подобрать по факту
   на живом прогоне.
3. Объединять ли шаг 6 (expired) в этот PR или вести параллельно с веткой
   `fix/expired-vacancies`, которая уже есть в worktree-списке.
