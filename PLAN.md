# Job Application Automation — План

## Что делаем
Skill `/apply` для Claude Code, который по ссылке или тексту вакансии генерирует полный пакет документов.

## Входные данные
- Вакансия: **URL** (парсинг страницы) или **текст** (вставка в чат)
- Базовое резюме: `D:/LearningProject/Claude/Ihar Petrasheuski CV Senior Frontend Developer (Angular) 2026.docx`

## Выходная структура
```
D:/LearningProject/Claude/Applications/
└── CompanyName_2026-04-06/
    ├── Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026.docx     ← EN, {Stack} = Angular/React/JavaScript по вакансии
    ├── Ihar Petrasheuski CV Senior Frontend Developer ({Stack}) 2026 PL.docx ← только если вакансия на польском
    ├── Cover_Letter_EN.docx
    ├── Cover_Letter_PL.docx
    ├── About_Me_EN.txt
    └── About_Me_PL.txt
```

## Логика генерации

### 1. Парсинг вакансии
- Извлечь: название компании, должность, требования, стек, ключевые слова
- Определить язык вакансии (EN/PL)

### 2. Резюме (ATS-оптимизация)
- **Keyword matching**: зеркалить термины из вакансии (70-80% совпадение)
- **Summary**: переписать под конкретную позицию
- **Skills**: переупорядочить — сначала то, что в вакансии
- **Experience bullets**: подчеркнуть релевантные достижения, добавить метрики
- **Заголовок**: адаптировать под позицию (напр. "Senior Frontend Developer (React)" если вакансия на React)
- **Формат**: DOCX, одноколоночный, стандартные заголовки секций
- Если вакансия на **польском** → генерим EN + PL версии
- Если на **английском** → только EN

### 3. Cover Letter (EN + PL)
- **250-350 слов**, 3-4 абзаца
- Структура:
  1. Сильный открывающий хук (не "I am writing to apply...")
  2. 2-3 proof points из опыта, привязанные к требованиям вакансии
  3. Почему эта компания (конкретика, не общие слова)
  4. Call to action
- Тон: профессиональный, но живой — не шаблонный AI-текст

### 4. "About Me" / Elevator Pitch (EN + PL)
- **3-5 предложений**
- Формула:
  1. Кто + стаж + специализация
  2. Ключевое достижение с метрикой
  3. Что отличает / экспертиза в домене
  4. (опционально) Что ищу / чем могу быть полезен
- Пример:
  > Senior Frontend Developer with 10+ years of experience building enterprise-scale Angular applications for fintech and banking. Built two production apps from scratch for 300+ German banks, handling complex data-driven workflows with AG Grid and NgRx. Experienced in owning frontend architecture end-to-end in cross-functional Agile teams of 10+. Open to Angular, React, and JavaScript roles in Poland and remote.

## Этапы реализации

### Этап 1 — Шаблоны ✏️
- [ ] Создать DOCX-шаблон резюме (чистый, ATS-friendly)
- [ ] Создать шаблон Cover Letter
- [ ] Согласовать формат "About Me"

### Этап 2 — Skill `/apply`
- [ ] Создать skill с двумя режимами ввода (URL / текст)
- [ ] Логика парсинга вакансии
- [ ] Логика переработки резюме
- [ ] Генерация Cover Letter (EN + PL)
- [ ] Генерация About Me (EN + PL)
- [ ] Сохранение в папку

### Этап 3 — Тестирование
- [ ] Прогнать на 2-3 реальных вакансиях
- [ ] Проверить ATS-совместимость (jobscan.co или аналог)
- [ ] Финальные правки

## Важные правила ATS
- Одноколоночная верстка, без таблиц/графиков/иконок
- Стандартные заголовки: Summary, Skills, Experience, Education
- Контактные данные в теле документа, НЕ в header/footer
- Акронимы + полные формы: "CI/CD (Continuous Integration / Continuous Deployment)"
- DOCX лучше парсится чем PDF (но генерим оба)
