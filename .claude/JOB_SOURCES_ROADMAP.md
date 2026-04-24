# План добавления job sources в hunter

Рецепт интеграции одного сайта: [commands/add-source.md](commands/add-source.md).

## Сколько ещё добавлять?

| Категория | Примерно сколько | Комментарий |
|-----------|------------------|-------------|
| **Tier A — API / RSS, без скрейпа** | **3** | Осталось из изначального списка «глобальных»: Himalayas, 4dayweek.io, We Work Remotely (RSS). Remote OK, Remotive, Arbeitnow уже сделаны. |
| **Tier B** | **0–1** | Remoteleaf — только если появится нормальная публичная дока / ключ. |
| **Tier C — скрейп / тяжёлая поддержка** | **5+** | Wellfound, Jobgether, EuroTechJobs, Relocate.me, Landing.jobs — без открытого feed; подключать только осознанно. |

Итого **реалистичный минимум «добить план»**: **ещё 3 источника** (tier A). Всё остальное — по желанию и трудозатратам, не «обязательный» объём.

---

## Уже в проекте (`hunter/sources/__init__.py`)

| Источник | `name` (для `/hunt`) | Стратегия |
|----------|----------------------|-----------|
| Just Join IT | `justjoin` | API |
| No Fluff Jobs | `nofluffjobs` | API |
| LinkedIn | `linkedin` | Playwright (опционально) |
| Bulldogjob | `bulldogjob` | HTML |
| Pracuj.pl | `pracuj` | `__NEXT_DATA__` |
| theprotocol.it | `theprotocol` | `__NEXT_DATA__` |
| Solid Jobs | `solidjobs` | RSS |
| Inhire | `inhire` | Playwright (опционально) |
| JobLeads | `jobleads` | HTML |
| Arbeitnow | `arbeitnow` | JSON API |
| Remotive | `remotive` | JSON API |
| Remote OK | `remoteok` | JSON API |

---

## Очередь tier A (рекомендуемый порядок)

1. **Himalayas** — `himalayas.app`: JSON API + RSS, есть OpenAPI. Много пересечений с другими remote-досками; зато фильтры (страна, remote) богатые.
2. **4dayweek.io** — JSON API v2 (`/api/v2/jobs`), хорошая дока, ниша 4-day week → меньше дубликатов с PL-досок.
3. **We Work Remotely** — `weworkremotely.com/remote-jobs.rss`: один большой RSS, реализация близка к [solidjobs.py](../hunter/sources/solidjobs.py); детали — `fetch_html` по `<link>`.

Для каждого нового источника после мержа: обновить эту таблицу, `CLAUDE.md`, `.env.example`, промпты (не использовать имя доски как `company_name`), при необходимости добавить `NEXT_SOURCE_*.md` с чеклистом.

---

## Вне scope (пока не планируем как «следующий по очереди»)

| Сайт | Почему |
|------|--------|
| Wellfound | Нет публичного feed, ToS |
| Jobgether | Нет публичного API/RSS |
| EuroTechJobs, Relocate.me, Landing.jobs | Нет удобного открытого API |
| Remoteleaf | Непрозрачный платный/закрытый API |

---

## Готовые пошаговые планы (файлы)

- [NEXT_SOURCE_REMOTIVE_PLAN.md](NEXT_SOURCE_REMOTIVE_PLAN.md) — выполнено  
- [NEXT_SOURCE_REMOTEOK_PLAN.md](NEXT_SOURCE_REMOTEOK_PLAN.md) — выполнено  

Следующий логичный отдельный план при желании: **Himalayas** или **4dayweek.io** (скопировать структуру с Remote OK / Remotive).

---

## Команды бота для проверки

```text
/hunt remoteok
/hunt remotive arbeitnow
/schedule
```
