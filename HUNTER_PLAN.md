# Autonomous Job Hunt Agent — Full Plan

## Что уже построено (Phase 1 — DONE)

### Скиллы Claude Code
- `/apply {url|text}` — полный пакет документов под одну вакансию
- `/batch {urls}` — пакетная обработка списка вакансий

### Файлы
- `generate_docs.py` — генерирует DOCX + PDF через python-docx + LibreOffice
- `tracker.xlsx` — мастер-таблица всех заявок (корень проекта)
- `.claude/commands/apply.md` — промпт-инструкция для /apply
- `.claude/commands/batch.md` — промпт-инструкция для /batch

### Что делает /apply
1. Парсит вакансию (URL или текст)
   - JustJoin.it → `https://api.justjoin.it/v1/offers/{slug}` (публичный API)
   - Остальные → WebFetch напрямую
2. Анализирует: компания, стек, язык (EN/PL), ключевые слова
3. ATS Gap Analysis — находит пробелы между вакансией и резюме
4. Итерирует резюме до 99% совпадения с ключевыми словами вакансии
5. Генерирует: резюме (EN + PL если нужно), cover letter EN+PL, about me EN+PL
6. Запускает `generate_docs.py` → DOCX + PDF
7. Обновляет `tracker.xlsx` (ATS%, To Learn, URL, дата)

### Структура выходной папки
```
Applications/{CompanyName}_{YYYY-MM-DD}/
    content.json
    Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026.docx + .pdf
    Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026 PL.docx + .pdf  ← PL вакансии
    Cover_Letter_EN.docx + .pdf
    Cover_Letter_PL.docx + .pdf
    About_Me_EN.txt
    About_Me_PL.txt
```

### tracker.xlsx колонки
`Date | Company | Job Title | Stack | ATS % | URL | Folder | Sent | Re-application | To Learn`
- ATS % — цветной: 🟢 ≥80%, 🟡 60-79%, 🔴 <60%
- URL — кликабельная ссылка
- Sent — ставишь вручную галочку
- Re-application — `+` если та же вакансия повторно
- To Learn — что реально надо подтянуть (из ATS анализа)

---

## Phase 2 — Autonomous Hunter (НЕ ПОСТРОЕНО)

### Архитектура

```
start_hunter.bat  (терминал, авто-рестарт при падении)
    └── hunter.py (cron: 08:00 / 13:00 / 19:00)
            ├── Поиск вакансий на 4 сайтах
            ├── Фильтрация + дедупликация (vs tracker.xlsx)
            ├── Уведомление в Telegram с кнопками [Apply] [Skip]
            └── При нажатии Apply:
                    ├── apply_agent.py → content.json → generate_docs.py
                    ├── Обновление tracker.xlsx
                    └── Telegram: "Docs ready → открой папку"
```

### Файлы для создания

| Файл | Назначение |
|------|-----------|
| `hunter.py` | Главный цикл: поиск + расписание + Telegram бот |
| `apply_agent.py` | Вызов Claude API → content.json → generate_docs.py |
| `start_hunter.bat` | Авто-рестарт при падении |
| `.env` | Токены и ключи (не в git) |

### start_hunter.bat
```batch
@echo off
:loop
echo [%date% %time%] Starting hunter...
python D:/LearningProject/Claude/hunter.py
echo [%date% %time%] Stopped. Restarting in 30s...
timeout /t 30 /nobreak
goto loop
```

### hunter.py — структура

**Расписание:**
```python
import schedule, time
schedule.every().day.at("08:00").do(run_hunt)
schedule.every().day.at("13:00").do(run_hunt)
schedule.every().day.at("19:00").do(run_hunt)
while True:
    schedule.run_pending()
    time.sleep(30)
```

**Источники вакансий:**

1. **JustJoin.it** (публичный JSON API, без авторизации):
   ```
   GET https://justjoin.it/api/offers
   Фильтры: marker_icon in [javascript, html], city=Wrocław OR remote=true
   Slug для конкретной вакансии: https://api.justjoin.it/v1/offers/{slug}
   ```

2. **LinkedIn** (WebFetch / публичный поиск):
   ```
   GET https://www.linkedin.com/jobs/search?keywords=angular+developer&location=Wroclaw%2C+Poland
   Парсим job cards из HTML
   ```

3. **NoFluffJobs**:
   ```
   GET https://nofluffjobs.com/api/search/posting?criteria=city%3Dwroclaw+remote%3Dtrue+category%3Dfrontend
   ```

4. **Pracuj.pl**:
   ```
   GET https://www.pracuj.pl/praca/angular%20developer;kw/wroclaw;wp?rd=30
   ```

**Фильтрация:**
- Title содержит: Angular / React / JavaScript / Frontend / TypeScript
- Уровень: Senior / Mid (исключить Junior/Intern/Trainee)
- Локация: Wrocław ИЛИ Remote ИЛИ Hybrid

**Дедупликация:**
- Читаем `tracker.xlsx` → собираем все URL
- Пропускаем вакансии, URL которых уже есть в трекере

**Формат уведомления в Telegram:**
```
Найдено 3 новых вакансии (06.04.2026 13:00)

1. Senior Angular Dev — Devapo
   Wrocław (Hybrid) | 110-140 PLN/h B2B
   https://linkedin.com/jobs/...
   [✅ Apply] [❌ Skip]

2. Frontend Developer (React) — 4Soft
   Remote | 18-24k PLN
   https://justjoin.it/...
   [✅ Apply] [❌ Skip]
```

**При нажатии кнопок:**
- `Apply` → запускает `apply_agent.py "{url}"`
- `Skip` → добавляет строку в tracker.xlsx со статусом `Skipped`

### apply_agent.py — структура

Автономный скрипт, использует Anthropic SDK напрямую (не Claude Code):
```
python apply_agent.py "https://linkedin.com/jobs/..."
```

Поток:
1. Получить текст вакансии (requests + парсинг)
2. Вызвать Claude API с промптом из apply.md (та же логика)
3. Записать `content.json` в папку компании
4. Запустить `python generate_docs.py {path_to_content.json}`
5. Добавить строку в tracker.xlsx
6. Отправить в Telegram: "Docs ready → Applications/Devapo_2026-04-06/"

### .env
```
TELEGRAM_BOT_TOKEN=xxxxxxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=xxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-...
```

### Зависимости для установки
```bash
pip install python-telegram-bot schedule anthropic openpyxl python-dotenv requests
```

---

## Phase 3 — LinkedIn Easy Apply автоматизация (НЕ ПОСТРОЕНО)

Новый скилл: `.claude/commands/linkedin-apply.md`

Использует `mcp__Claude_in_Chrome__*` инструменты:
1. Навигация на URL вакансии
2. Клик "Easy Apply"
3. Заполнение формы: имя, email, телефон из профиля Игоря
4. Прикрепление PDF резюме + cover letter из `Applications/{Company}_{date}/`
5. Скриншот формы перед отправкой → в Telegram для финального подтверждения
6. После подтверждения: Submit
7. Обновление tracker.xlsx: статус → `Applied`

Запускается вручную из Claude Code когда Chrome открыт и залогинен в LinkedIn.

---

## Настройка Telegram бота (один раз, вручную)

1. Написать @BotFather в Telegram → `/newbot`
2. Дать имя боту (напр. `IharJobHunterBot`)
3. Скопировать токен → в `.env` как `TELEGRAM_BOT_TOKEN`
4. Запустить бота → написать ему любое сообщение
5. Получить `chat_id`:
   ```
   GET https://api.telegram.org/bot{TOKEN}/getUpdates
   ```
6. Скопировать `chat_id` → в `.env` как `TELEGRAM_CHAT_ID`

---

## Профиль Игоря (для apply_agent.py)

```
Имя: Ihar Petrasheuski (also known as Igor Pietraszewski)
Контакты: +48 571 525 110 | igrflex@gmail.com | linkedin.com/in/ijerweb | Wrocław, Poland
Опыт: 10+ лет frontend, специализация Angular
Стек: Angular (2-19), NgRx, RxJS, Signals, Nx Monorepo, AG Grid, TypeScript, JavaScript
Инструменты: Jest, Jasmine, Cypress, Git, Jenkins, Webpack, Node.js
Домены: Fintech, banking, AI procurement, e-commerce, healthcare
Языки: English (Fluent), Russian (Native), Polish (B1)
Цели: Angular/React/JavaScript roles, Wrocław + Remote
```

---

## Порядок реализации Phase 2

1. Создать `.env` (вручную заполнить токены)
2. `pip install python-telegram-bot schedule anthropic python-dotenv requests`
3. Написать `hunter.py`:
   - Сначала только JustJoin (самый простой — публичный API)
   - Протестировать Telegram уведомления
   - Добавить остальные сайты
4. Написать `apply_agent.py` с Anthropic SDK
5. Создать `start_hunter.bat`
6. Тестирование полного цикла: Hunt → Telegram → Apply → Docs → Tracker
7. Phase 3: LinkedIn Easy Apply через Chrome MCP
