# План: параллельная проверка истёкших вакансий

## Цель
Ускорить `/check_expired` за счёт параллельных HTTP-запросов.
Вместо 1 запроса в 1.5 сек → N запросов одновременно, ограниченных по домену.

---

## Текущее поведение
`hunter/expired_to_send_check.py` — цикл:
```
for item in to_check:
    fetch_job_text(item["url"])   # блокирующий
    await asyncio.sleep(1.5)      # задержка между запросами
```
200 строк × 1.5 сек = ~5 минут.

---

## Целевое поведение
- Максимум `MAX_CONCURRENT = 10` запросов одновременно (глобально)
- Максимум `DOMAIN_CONCURRENCY = 2` запросов к одному домену одновременно
- Задержка `DOMAIN_DELAY = 1.0 сек` между запросами к одному домену
- Ожидаемое ускорение: x4–x6 (зависит от распределения доменов)

---

## Шаги реализации

### Шаг 1 — Утилита: семафоры по доменам
Файл: `hunter/expired_to_send_check.py`

Добавить:
```python
from urllib.parse import urlparse
from collections import defaultdict

MAX_CONCURRENT   = 10   # глобальный лимит параллельных запросов
DOMAIN_CONCURRENCY = 2  # макс одновременных запросов к одному домену
DOMAIN_DELAY     = 1.0  # сек между запросами к одному домену (per-domain)

_global_sem: asyncio.Semaphore | None = None
_domain_sems: dict[str, asyncio.Semaphore] = {}
_domain_locks: dict[str, asyncio.Lock] = {}   # для DOMAIN_DELAY

def _get_domain(url: str) -> str:
    return urlparse(url).hostname or url

async def _fetch_with_limits(url: str) -> str:
    """Fetch с глобальным + доменным семафором и задержкой между запросами к домену."""
    domain = _get_domain(url)

    if domain not in _domain_sems:
        _domain_sems[domain] = asyncio.Semaphore(DOMAIN_CONCURRENCY)
        _domain_locks[domain] = asyncio.Lock()

    async with _global_sem:
        async with _domain_sems[domain]:
            result = await asyncio.to_thread(fetch_job_text, url)
            # Задержка применяется через lock чтобы не перекрывались
            async with _domain_locks[domain]:
                await asyncio.sleep(DOMAIN_DELAY)
            return result
```

### Шаг 2 — Параллельный цикл в `run_check()`
Заменить последовательный `for` + `asyncio.sleep` на `asyncio.gather` с задачами:

```python
global _global_sem
_global_sem = asyncio.Semaphore(MAX_CONCURRENT)

async def _check_one(item: dict) -> dict:
    """Проверить одну вакансию. Возвращает {status: "expired"|"alive"|"error", ...}"""
    try:
        text = await _fetch_with_limits(item["url"])
        if is_job_expired(text):
            return {**item, "status": "expired"}
        return {**item, "status": "alive"}
    except requests.exceptions.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return {**item, "status": "expired", "reason": "404"}
        return {**item, "status": "error", "error": str(e)}
    except Exception as e:
        if "jobleads.com" in item["url"]:
            return {**item, "status": "alive"}   # Cloudflare — skip
        return {**item, "status": "error", "error": str(e)}

tasks = [_check_one(item) for item in to_check]
results = await asyncio.gather(*tasks)
```

### Шаг 3 — Прогресс в реальном времени
`asyncio.gather` не даёт прогресс по умолчанию.
Использовать `asyncio.as_completed` или `asyncio.Queue` для коллбэка:

```python
done = 0
expired_count = 0

async def _check_one_tracked(item):
    nonlocal done, expired_count
    result = await _check_one(item)
    done += 1
    if result["status"] == "expired":
        expired_count += 1
    if progress_cb and done % PROGRESS_EVERY == 0:
        await progress_cb(f"⏳ {done}/{total} проверено — ⏭ истекло: {expired_count}")
    return result
```

### Шаг 4 — Применить результаты к воркбуку
После `gather` — один проход по результатам, пометить EXPIRED в листе:

```python
for res in results:
    if res["status"] == "expired":
        expired.append(res)
        cell = ws.cell(res["row"], sent_col, value="EXPIRED")
        cell.fill = PatternFill("solid", fgColor="FCE4D6")
        cell.font = Font(name="Calibri", size=11, color="9C0006", bold=True)
    elif res["status"] == "alive":
        alive += 1
    else:
        errors.append(res)
```

### Шаг 5 — CLI-скрипт `tools/check_expired_to_send.py`
CLI синхронный — завернуть в `asyncio.run()`:

```python
import asyncio
from hunter.expired_to_send_check import run_check

async def main_async():
    ...  # переиспользовать run_check с progress_cb=None

asyncio.run(main_async())
```
Либо CLI-скрипт просто вызывает `asyncio.run(run_check())` напрямую.

---

## Конфигурация (добавить в `hunter/config.py`)
```python
EXPIRED_CHECK_CONCURRENCY: int = int(os.getenv("EXPIRED_CHECK_CONCURRENCY", "10"))
EXPIRED_CHECK_DOMAIN_LIMIT: int = int(os.getenv("EXPIRED_CHECK_DOMAIN_LIMIT", "2"))
EXPIRED_CHECK_DOMAIN_DELAY: float = float(os.getenv("EXPIRED_CHECK_DOMAIN_DELAY", "1.0"))
```

---

## Ожидаемый результат
| | Сейчас | После |
|---|---|---|
| 200 строк | ~5 мин | ~1 мин |
| 50 строк | ~75 сек | ~20 сек |
| Риск бана | низкий | низкий (domain limit) |

---

## Файлы для изменения
1. `hunter/expired_to_send_check.py` — основная логика (шаги 1–4)
2. `tools/check_expired_to_send.py` — CLI обёртка (шаг 5)
3. `hunter/config.py` — новые константы (шаг 6)
