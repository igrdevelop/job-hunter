# План добавления job sources в hunter

Рецепт интеграции одного сайта: [commands/add-source.md](commands/add-source.md).

## Сколько ещё добавлять?

| Категория | Примерно сколько | Комментарий |
|-----------|------------------|-------------|
| **Tier A — API / RSS, без скрейпа** | **1** | Осталось: We Work Remotely (RSS). Remote OK, Remotive, Arbeitnow, Himalayas, 4dayweek.io уже сделаны. |
| **Tier B** | **0–1** | Remoteleaf — только если появится нормальная публичная дока / ключ. |
| **Tier C — скрейп / тяжёлая поддержка** | **5+** | Wellfound, Jobgether, EuroTechJobs, Relocate.me, Landing.jobs — без открытого feed; подключать только осознанно. |

Итого **реалистичный минимум «добить план»**: **ещё 1 источник** (tier A). Всё остальное — по желанию и трудозатратам, не «обязательный» объём.

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
| Himalayas | `himalayas` | JSON API |
| 4dayweek.io | `fourdayweek` | JSON API |

---

## Очередь tier A (рекомендуемый порядок)

1. **We Work Remotely** — `weworkremotely.com/remote-jobs.rss`: один большой RSS, реализация близка к [solidjobs.py](../hunter/sources/solidjobs.py); детали — `fetch_html` по `<link>`.

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

Следующий логичный отдельный план при желании: **We Work Remotely** (скопировать структуру с Solid.Jobs).

---

## Команды бота для проверки

```text
/hunt remoteok
/hunt himalayas
/hunt fourdayweek
/hunt remotive arbeitnow
/schedule
```
