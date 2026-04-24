# Claude Code Automation Plan

Файл создан: 2026-04-21  
Статус: **ВЫПОЛНЕНО**

---

## Задачи

### ⚡ Хуки (Hooks)

- [x] **1. Скрипт-помощник: синтаксис** — `.claude/hooks/syntax_check.py`  
  PostToolUse: проверяет синтаксис отредактированного `.py` файла через `py_compile`

- [x] **2. Скрипт-помощник: защита файлов** — `.claude/hooks/block_protected.py`  
  PreToolUse: блокирует редактирование `.env` и `tracker.xlsx`

- [x] **3. Регистрация хуков в settings.local.json**  
  Добавлен блок `hooks` с двумя правилами (PostToolUse + PreToolUse)  
  Оба скрипта протестированы pipe-тестом: syntax_check → `[OK]`, block_protected → `exit 1` на `.env`

---

### 🔌 MCP-серверы

- [x] **4. context7** — документация Python-библиотек в реальном времени  
  Добавлен в `~/.claude.json` для проекта D:\LearningProject\Claude

- [x] **5. GitHub MCP** — управление репозиторием из сессии  
  Добавлен в `~/.claude.json` для проекта D:\LearningProject\Claude

---

### 🎯 Скиллы (Skills)

- [x] **6. `/debug-scraper`** — `.claude/skills/debug-scraper/SKILL.md`  
  Диагностика сломанного скрапера: fetch → inspect → compare → fix

- [x] **7. `/release-notes`** — `.claude/skills/release-notes/SKILL.md`  
  Генерация changelog из git log develop..master → запись в DEPLOY.md

---

### 🤖 Субагенты (Subagents)

- [x] **8. `scraper-health-checker`** — `.claude/agents/scraper-health-checker.md`  
  Параллельная проверка всех скраперов: возвращают ли они результаты

---

## Прогресс

| # | Элемент | Статус |
|---|---------|--------|
| 1 | syntax_check.py | ✅ |
| 2 | block_protected.py | ✅ |
| 3 | Хуки в settings.local.json | ✅ |
| 4 | MCP context7 | ✅ |
| 5 | MCP GitHub | ✅ |
| 6 | Скилл debug-scraper | ✅ |
| 7 | Скилл release-notes | ✅ |
| 8 | Субагент scraper-health-checker | ✅ |

---

## Файлы созданы/изменены

```
.claude/
  settings.local.json          ← добавлены hooks (PostToolUse + PreToolUse)
  hooks/
    syntax_check.py            ← проверка синтаксиса .py после редактирования
    block_protected.py         ← блокировка .env и tracker.xlsx
  skills/
    debug-scraper/SKILL.md     ← /debug-scraper скилл
    release-notes/SKILL.md     ← /release-notes скилл
  agents/
    scraper-health-checker.md  ← субагент для проверки скраперов
  AUTOMATION_PLAN.md           ← этот файл
```

MCP-серверы добавлены в `~/.claude.json` (user-level, проект D:\LearningProject\Claude):
- `context7` → `npx -y @upstash/context7-mcp`
- `github` → `npx -y @modelcontextprotocol/server-github`

> **Примечание:** MCP-серверы и хуки вступят в силу после перезапуска Claude Code  
> (или откройте `/hooks` в меню для перезагрузки хуков без перезапуска)
