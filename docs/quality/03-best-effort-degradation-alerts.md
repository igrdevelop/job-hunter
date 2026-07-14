# 03 — best_effort: алерты о молчаливой деградации подсистем

**Приоритет:** P1 · **Усилие:** ~1 день · **Ветка:** `feat/best-effort-degradation-alerts`

## Для чего апдейт

В коде **292** блока `except Exception` — это осознанный контракт
(«Sheets/Drive/Telegram/shadow никогда не роняют apply», задокументировано
вплоть до ruff-ignore S110/S112). Контракт правильный, но у него есть цена:
деградация накапливается **молча**. Реальный кейс 2026-07-13: протухший
in-memory Drive-токен → каждый upload тихо падал часами, узнали от владельца
(«файлы не появляются на диске. и вообще нет»).

Для двух подсистем самодиагностика уже построена точечно:
- `hunter/source_health.py` — алерт, когда рабочий скрейпер даёт 0 N раз подряд;
- `hunter/oauth_alert.py` — алерт на протухший Google-токен с cooldown-дедупом.

Этот пункт **генерализует паттерн** на все best-effort подсистемы: Sheets
mirror, Drive upload, delivery, outreach, dual-shadow, cost/verdict writers.

## Как именно будет происходить

Новый модуль `hunter/best_effort.py`:

```python
@contextmanager
def best_effort(subsystem: str, *, threshold: int = 3, notify=None):
    """Проглатывает исключение (контракт сохраняется), но считает
    ПОДРЯД идущие фейлы per-subsystem. На пороге — один Telegram-алерт
    (cooldown как в oauth_alert), успех сбрасывает счётчик."""
```

- Счётчики — в SQLite (`hunter/db.py`, новая таблица `subsystem_health
  (subsystem TEXT PK, consecutive_failures INT, last_error TEXT,
  last_alert_at TEXT)`), а не в памяти: apply-пайплайн живёт в сабпроцессах,
  фейлы должны суммироваться между процессами (тот же довод, что у
  source_health).
- Алерт: `⚠️ {subsystem}: {N} подряд сбоев, последний: {err}` через
  существующий `_tg_notify`-механизм; cooldown 6ч на подсистему, чтобы не
  спамить. Восстановление (успех после алерта) — одно сообщение
  `✅ {subsystem} восстановился`.
- Существующие try/except **не выпиливаются** — обёртка ставится вокруг или
  вместо них точечно, семантика «никогда не роняем apply» не меняется.

Порядок внедрения (по одной подсистеме на коммит):
1. `hunter/gdrive_sync.py` — upload_application_folder / upload_shadow_folder
   / upload_missing_folders (именно тут был инцидент);
2. `hunter/gsheets_sync.py` — mirror_new_row / resync;
3. `hunter/delivery.py` — deliver_apply_now (обе стадии);
4. `hunter/outreach.py`, `hunter/dual_apply.py` (shadow),
   `hunter/cost_writer.py` / `hunter/verdict_writer.py`.

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `hunter/best_effort.py` | **Новый**: contextmanager + счётчики + алерт с cooldown |
| `hunter/db.py` | Таблица `subsystem_health` (idempotent CREATE) |
| `hunter/gdrive_sync.py`, `gsheets_sync.py`, `delivery.py`, `outreach.py`, `dual_apply.py`, `cost_writer.py`, `verdict_writer.py` | Точечные обёртки `with best_effort("...")` вокруг существующих best-effort блоков |
| `hunter/commands/status.py` (опц.) | `/status` показывает подсистемы с ненулевым счётчиком фейлов |
| `tests/test_best_effort.py` | **Новый**: порог/сброс/cooldown/восстановление/межпроцессное суммирование (через tmp-БД) |
| `CLAUDE.md` | Repository Layout + абзац в «Important Rules»: новый best-effort код оборачивается в `best_effort()` |

## Критерий готовности

- Юнит-тесты: 3 фейла подряд → ровно один алерт; успех → сброс + recovery-
  сообщение; повторные фейлы внутри cooldown → без второго алерта.
- Ручная проверка на проде: временно сломать Drive-токен (переименовать
  token-файл) → в течение ~1.5ч (3 фейла бэкфилла по 30 мин) приходит алерт.

## Риски

- Ложные алерты на «легитимно редких» операциях (shadow бывает выключен) —
  порог считается по подряд-фейлам, не по времени, так что неактивная
  подсистема алерт не даёт.
- Не превращать в метрики-платформу: никаких дашбордов/экспортеров — правило
  владельца против спекулятивных слоёв. Один модуль, одна таблица, один алерт.
