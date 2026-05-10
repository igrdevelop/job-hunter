# План добавления job sources в hunter

Рецепт интеграции одного сайта: [commands/add-source.md](commands/add-source.md).

## Сколько ещё добавлять?

| Категория | Примерно сколько | Комментарий |
|-----------|------------------|-------------|
| **Tier A — API / RSS, без скрейпа** | **0** | Запланированные источники закрыты: в т.ч. We Work Remotely (RSS). Дальше — по желанию (tier B/C). |
| **Tier B** | **0** | Remoteleaf: интеграция через HTML-листинг (см. `remoteleaf.py`); публичного JSON нет. |
| **Tier C — скрейп / тяжёлая поддержка** | **5+** | Wellfound, Jobgether, EuroTechJobs, Relocate.me, Landing.jobs — без открытого feed; подключать только осознанно. |

Итого **минимальный tier A** выполнен. Дополнительные доски — по желанию (tier B/C), не обязательный объём.

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
| We Work Remotely | `weworkremotely` | RSS |
| RemoteLeaf | `remoteleaf` | HTML (категория + `?skills=` + `&page=`) |

---

## Очередь tier A

*Пусто — рекомендуемый минимум tier A исчерпан.*

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

Дальше при желании: tier B (Remoteleaf и т.п.) или tier C из таблицы «Вне scope».

---

## Команды бота для проверки

```text
/hunt remoteok
/hunt himalayas
/hunt fourdayweek
/hunt weworkremotely
/hunt remoteleaf
/hunt remotive arbeitnow
/schedule
```
