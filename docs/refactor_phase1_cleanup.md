# Refactoring Phase 1 — Cleanup

**Цель:** Убрать мусор, устаревшие документы и дублирование кода без риска сломать рабочий пайплайн.
**Ветка:** `develop`
**Приоритет:** LOW risk, высокая ценность — делается быстро, уменьшает когнитивную нагрузку на агентов.

---

## Задачи

### 1.1 Удалить устаревшие doc-файлы

**Статус:** ✅ Сделано

**Почему устарели:**

| Файл | Причина |
|------|---------|
| `PLAN.md` | Phase 1 (`/apply` skill) помечена как "в процессе" — давно реализована |
| `HUNTER_PLAN.md` | Hunter bot описан как "НЕ ПОСТРОЕНО" — полностью работает |
| `EXPIRED_PLAN.md` | План expired check — реализован в `hunter/expired_check.py` |
| `PROJECT_REVIEW_AND_REFACTOR_PLAN.md` | Все TASK-01...05 выполнены, зафиксировано в разделе 8 того же файла |
| `WEBSITE_PLAN.md` | Не относится к этому проекту (план личного сайта) |

**Команды:**
```bash
git rm PLAN.md HUNTER_PLAN.md EXPIRED_PLAN.md PROJECT_REVIEW_AND_REFACTOR_PLAN.md WEBSITE_PLAN.md
```

**Проверка:**
```bash
git status  # только эти 5 файлов в staged
```

**Результат выполнения:**
- Дата: 2026-05-13
- Кто: Claude (agent)
- Заметки: commit `7526acb` — 5 файлов удалены через `git rm`

---

### 1.2 Удалить дебаг-артефакты с диска

**Статус:** ✅ Сделано

**Файлы:**
- `_probe2.py`, `_probe3.py`, `_probe_bulldogjob.py` — ручные пробники скраперов, уже не нужны
- `tracker_broken.xlsx` — сломанная копия трекера, оставлена для диагностики

**Важно:** Эти файлы уже в `.gitignore` (паттерны `_*.py` и `tracker_broken.xlsx`),
поэтому они **не трекаются git-ом** — удалить нужно только с диска.

**Команды:**
```bash
# Windows PowerShell
Remove-Item _probe2.py, _probe3.py, _probe_bulldogjob.py, tracker_broken.xlsx
```

**Проверка:**
```bash
git status  # не должно появиться ничего нового
```

**Результат выполнения:**
- Дата: 2026-05-13
- Кто: Claude (agent)
- Заметки: `_probe2.py`, `_probe3.py`, `_probe_bulldogjob.py`, `tracker_broken.xlsx` удалены с диска через `rm`. Файлы были в `.gitignore` — `git status` чистый.

---

### 1.3 Проверить .gitignore и __pycache__

**Статус:** ✅ Уже сделано (не требует действий)

`.gitignore` уже содержит:
```
__pycache__/
*.pyc
*.pyo
_*.py
tracker_broken.xlsx
```

`git ls-files` показывает: `__pycache__`, `.pyc`, `_probe*.py` **не трекаются**.

---

### 1.4 Унифицировать запуск apply_agent из telegram_bot.py

**Статус:** ✅ Сделано

**Проблема:**

В проекте сейчас **два** разных механизма запуска `apply_agent.py` как subprocess:

**`hunter/services/apply_service.py:run_apply_agent_subprocess()`** — используется в `main.py`:
- Принимает `Job` объект
- Возвращает `ApplyOutcome` (`ok` / `fail` / `manual`)
- Поддерживает JobLeads `--company` / `--title` аргументы
- Обрабатывает exit code 44 (MANUAL flow)

**`hunter/telegram_bot.py:_run_apply_agent()`** (строки 634–700) — используется внутри bot-а:
- Принимает `url: str`, `force: bool`, `paste_file: Optional[str]`
- Не возвращает результат (fire-and-forget через `asyncio.create_task`)
- Шлёт Telegram-уведомления об ошибках напрямую через `_tg_notify`
- Поддерживает `--paste-file` флаг (paste flow)

**Эти две функции решают разные задачи** — прямая замена невозможна без рефакторинга.

**Правильный подход:**

Расширить `apply_service.py`, добавив вариант для URL-based вызовов (без Job объекта),
и перевести `telegram_bot.py` на него.

```python
# apply_service.py — добавить:
async def run_apply_agent_for_url(
    url: str,
    timeout_sec: int,
    apply_agent_path: Path,
    python_executable: str,
    force: bool = False,
    paste_file: Optional[str] = None,
) -> ApplyOutcome:
    """URL-based variant for manual Telegram triggers (no Job object needed)."""
    cmd = [python_executable, str(apply_agent_path)]
    if url:
        cmd.append(url)
    if force:
        cmd.append("--force")
    if paste_file:
        cmd.extend(["--paste-file", paste_file])
    # ... общий код с run_apply_agent_subprocess
```

Затем в `telegram_bot.py` заменить `_run_apply_agent()` на вызов сервиса.

**Файлы затронуты:**
- `hunter/services/apply_service.py` — добавить `run_apply_agent_for_url()`
- `hunter/telegram_bot.py` — удалить `_run_apply_agent()`, заменить 7 call-sites

**Проверка после изменений:**
```bash
python -m compileall hunter/
pytest tests/test_apply_service.py
pytest tests/test_hunter_apply_agent.py
```

Также: ручной тест через Telegram — отправить URL вакансии боту.

**Результат выполнения:**
- Дата: 2026-05-13
- Кто: Claude (agent)
- Заметки: commit `265d87e`. Добавлена `run_apply_agent_for_url()` в `apply_service.py`; `_run_apply_agent()` в `telegram_bot.py` теперь тонкий враппер над сервисом. Все 8 тестов прошли (`test_apply_service.py` + `test_hunter_apply_agent.py`).

---

## Порядок выполнения

```
1.1 → 1.2 → (1.3 пропустить) → 1.4
```

Задачи 1.1 и 1.2 независимые и безопасные — делать в одном коммите.
Задача 1.4 требует тестирования — делать отдельным коммитом.

**Commit messages:**
```
chore: remove stale docs and debug artifacts (Phase 1.1, 1.2)
refactor: unify apply_agent subprocess launch via apply_service (Phase 1.4)
```

---

## Итог фазы

После выполнения всех задач:
- [ ] 5 устаревших MD-файлов удалены из репозитория
- [ ] 4 дебаг-артефакта удалены с диска
- [ ] Один единственный путь запуска apply_agent (через `apply_service.py`)
- [ ] Обновить чекбоксы в `CLAUDE.md` раздел "Phase 1 — Cleanup"
- [ ] Добавить запись в Agent Work Log в `CLAUDE.md`
