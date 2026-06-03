# План: bootstrap дедупа из Google Sheets

**Ветка:** `fix/bootstrap-dedup-from-sheets` (от `origin/master` @ `bc1fbde`)
**Дата:** 2026-06-03
**Контекст:** см. [DUPLICATE_INVESTIGATION.md](DUPLICATE_INVESTIGATION.md)
**Режим запуска (подтверждено владельцем):** одно окружение — Docker на сервере.

---

## 1. Цель

Сделать так, чтобы локальная `tracker.db` **самовосстанавливала знание о ранее обработанных
вакансиях из Google-таблицы**. Тогда после рестарта контейнера (или пересоздания тома) дедуп
перестаёт быть «слепым» и бот не переобрабатывает живые вакансии.

**Не делаем:** переход на Postgres / возврат к xlsx-файлу. SQLite остаётся; при одном окружении
он консистентен. Чиним только дыру «пустая БД после рестарта».

---

## 2. Корневая причина (кратко)

- Дедуп читает только `tracker.db` (`get_known_urls()` в `hunter/main.py`, шаг 3).
- Google-таблица — одностороннее зеркало; обратная синхронизация `pull_full_snapshot()` →
  `_apply_pull_delta_db()` (`hunter/gsheets_sync.py:297`) **сопоставляет строки только по `ID`
  и обновляет** поля `Sent/To Learn/Re-application` у уже существующих в БД записей —
  **никогда не вставляет** строки, которых в БД нет.
- ⇒ если БД свежая/пустая, таблица не может «напомнить» боту об URL. Дедуп слепой.

**Доказательство в данных:** 41 из 50 повторных строк имеют пустой флаг `Re-application`
(бот ставит `+`, только если URL уже был в его БД).

---

## 3. Дизайн-решение

Расширить **один** код-путь — `pull_full_snapshot()` — чтобы он, помимо обновления совпавших
по `ID` строк, **вставлял строки, присутствующие в таблице, но отсутствующие в `tracker.db`**.
Затем вызвать `pull_full_snapshot()` **один раз при старте** (в `_post_init`), чтобы свежая БД
дочитывала историю до первого `/hunt`.

Почему так, а не отдельная функция bootstrap:
- периодический `scheduled_gsheets_pull` тоже начнёт самовосстанавливать БД (бесплатный бонус);
- одна точка логики merge Sheets→DB, меньше дублирования;
- стартовый вызов — это просто «прогон pull один раз».

### Правила вставки (безопасность)

Для каждой строки из таблицы, которой **нет** в БД (ни по `ID`, ни по `url_norm`):

| Поле | Источник | Примечание |
|------|----------|-----------|
| `id` | из таблицы (колонка `ID`) | если пусто — строку **пропускаем** (несинхронизируемая); логируем счётчик |
| `url`, `url_norm` | `URL` из таблицы, `url_norm = normalize_url(URL)` | url_norm — ключ дедупа |
| `date, company, title, stack, ats_status, folder, sent, reapplication, to_learn, drive_url, confirmation, answer` | соответствующие колонки | как есть |
| `sheets_row` | индекс строки из `read_all` (1-based) | для последующего resync |
| `sheets_dirty` | `0` | строка пришла из таблицы, она «чистая» |

- Вставка через `INSERT OR IGNORE` по `PRIMARY KEY id` — гонок и перезаписи не будет.
- Существующие строки (совпадение по `id` **или** по `url_norm`) **не трогаем** — это работа
  существующего conflict-matrix в `_apply_pull_delta_db` (он обновляет `Sent/To Learn/Re-app`).
- Дедуп при вставке также по `url_norm`: если в таблице две строки с одним url_norm (исторические
  дубли — у нас их 34), в БД попадёт только первая; остальные игнорируются. Это и нужно.

---

## 4. Изменения по файлам

### 4.1 `hunter/tracker.py` — новая функция `insert_pulled_rows()`

Рядом с `apply_pull_updates()` (после строки ~811). Сигнатура и поведение:

```python
def insert_pulled_rows(rows: list[tuple[int, dict]]) -> int:
    """Insert Sheets rows that are absent from the DB (dedup self-heal).

    rows: list of (sheet_row_index, row_dict) as returned by gsheets_client.read_all.
    For each row whose ID is non-empty AND neither its ID nor its url_norm already
    exists in the DB, insert a fresh applications row (sheets_row set, sheets_dirty=0).
    Returns count inserted.
    """
```

Реализация:
- собрать существующие `id` и `url_norm` одним `SELECT id, url_norm FROM applications`;
- для каждой строки: пропустить если нет `ID`; вычислить `url_norm = normalize_url(URL)`;
  пропустить если `id in existing_ids` или (`url_norm` непустой и `url_norm in existing_norms`);
  иначе `INSERT OR IGNORE` со всеми колонками (по образцу `db.migrate_from_excel`,
  `hunter/db.py:301-320`);
- добавлять только что вставленный `url_norm`/`id` в локальные множества, чтобы внутри одного
  вызова не задвоить;
- считать `inserted` через `conn.total_changes` или `SELECT changes()`.

Маппинг ключей dict → колонки БД — те же, что в `read_all_tracker_rows()` наоборот
(`Date→date`, `Job Title→title`, `ATS %→ats_status`, `Re-application→reapplication`, …).

### 4.2 `hunter/gsheets_sync.py` — вставка внутри `pull_full_snapshot()`

В `pull_full_snapshot()` (строка ~352) после получения `sheets_rows` и **до/после**
`_apply_pull_delta_db`:

```python
from hunter.tracker import insert_pulled_rows
...
inserted = await asyncio.to_thread(insert_pulled_rows, sheets_rows)
if inserted:
    log.info("gsheets pull_full_snapshot: inserted %d missing rows into DB", inserted)
```

- импорт `insert_pulled_rows` добавить в существующий блок `from hunter.tracker import (...)`
  (строки 32-42);
- вернуть число в результирующем dict: добавить ключ `"inserted": inserted` к возврату
  (`{"pulled":..., "updated":..., "inserted":..., "errors":...}`);
- порядок: сначала `insert_pulled_rows` (восстановить отсутствующие), потом
  `_apply_pull_delta_db` (обновить поля по conflict-matrix) — так свежевставленные строки
  тоже получат корректные `Sent/To Learn` из таблицы в том же проходе.

### 4.3 `hunter/telegram_bot.py` — вызвать pull один раз при старте

В `_post_init()` (строка ~167), **после** успешного `init_or_load_spreadsheet` и **до**
`cache.load_from_db()` (строка 185), чтобы кэш увидел восстановленные строки:

```python
# Self-heal dedup state: pull Sheets → DB (inserts rows missing locally).
try:
    pull_res = await gsheets_sync.pull_full_snapshot()
    logger.info(
        "[startup] gsheets pull: pulled=%s inserted=%s updated=%s",
        pull_res.get("pulled"), pull_res.get("inserted"), pull_res.get("updated"),
    )
except Exception as e:
    logger.warning("[startup] gsheets pull failed: %s", e)
```

Обернуть в проверку `GSHEETS_ENABLED` (внутри `pull_full_snapshot` уже есть `_ready()` guard —
при выключенных Sheets вернёт нули, так что отдельная проверка опциональна).

### 4.4 Опционально — счётчик в `/gsheets_status`

Добавить в отчёт `/gsheets_status` строку «rows in DB / rows in Sheet», чтобы расхождение
было видно сразу. (Не обязательно для фикса; полезно для мониторинга.)

---

## 5. Тесты (`tests/test_gsheets_sync.py` или новый `tests/test_bootstrap_dedup.py`)

1. `test_insert_pulled_rows_inserts_missing` — БД пустая, на входе 3 Sheets-строки с ID/URL →
   вставлено 3; `get_known_urls()` содержит их url_norm.
2. `test_insert_pulled_rows_skips_existing_by_id` — строка с тем же `ID` уже в БД → 0 вставок,
   существующая не перезаписана.
3. `test_insert_pulled_rows_skips_existing_by_url_norm` — в БД есть строка с тем же url_norm,
   но другим ID → 0 вставок (дедуп по URL).
4. `test_insert_pulled_rows_dedups_within_batch` — две Sheets-строки с одним url_norm →
   вставлена ровно одна.
5. `test_insert_pulled_rows_skips_blank_id` — строка без ID пропускается, считается отдельно.
6. `test_pull_full_snapshot_inserts_then_updates` — интеграционно: pull вставляет недостающую
   строку и применяет conflict-matrix к существующей за один проход (мок `read_all`).
7. Регрессия дедупа: после `insert_pulled_rows` повторный `run_hunt` по тем же URL даёт
   `dup_url == N`, `new_jobs == 0` (мок источника + мок Sheets).

Прогон: `pytest tests/ -q` — все существующие (976) + новые должны проходить.
Синтаксис: `python -m compileall hunter`.

---

## 6. Действия на стороне сервера (ops, без кода)

Эти два пункта закрывают причины №1 и №2 из исследования и обязательны независимо от кода:

1. **Гарантировать персистентность `tracker.db`.** На хосте до старта контейнера:
   ```bash
   test -f tracker.db && file tracker.db   # должно быть "SQLite 3.x database", не каталог
   # если каталог/нет файла:
   #   docker compose down
   #   rm -rf tracker.db && touch tracker.db   # бот создаст схему сам
   #   docker compose up -d
   ```
   После пары applies проверить, что размер `tracker.db` на хосте растёт.

2. **Локальные запуски не пишут в общую таблицу.** Для preview локально:
   `GSHEETS_ENABLED=false` и отдельный `APPLICATIONS_DIR`. Тогда строки `D:/...` в общей
   таблице не появляются.

---

## 7. Разовая чистка существующих дублей (отдельный шаг)

В таблице сейчас 50 лишних строк по 34 URL. После мерджа фикса — разово почистить:
- скрипт `tools/dedup_sheet.py` (новый, опционально): читает `read_all`, группирует по
  `normalize_url(URL)`, оставляет «лучшую» строку (с заполненным `Sent`, иначе самую раннюю),
  остальные помечает/удаляет;
- **или** вручную в таблице (34 группы — обозримо), затем `/sync_sent` для выравнивания БД.
Делать **после** деплоя фикса, иначе дубли наберутся снова.

---

## 8. Порядок работ

1. [ ] `insert_pulled_rows()` в `hunter/tracker.py` + юнит-тесты 1-5.
2. [ ] Встроить вставку в `pull_full_snapshot()` (`hunter/gsheets_sync.py`) + тест 6.
3. [ ] Стартовый вызов pull в `_post_init` (`hunter/telegram_bot.py`).
4. [ ] Регресс-тест дедупа (тест 7).
5. [ ] `python -m compileall hunter` + `pytest tests/ -q`.
6. [ ] Обновить CLAUDE.md: раздел про pull (теперь insert+update), Agent Work Log.
7. [ ] Коммит, пуш ветки, PR в `master`.
8. [ ] Ops: пункты 6.1 и 6.2 на сервере.
9. [ ] После деплоя — разовая чистка дублей (раздел 7).

---

## 9. Риски и крайние случаи

- **Мусорные строки в таблице** попадут в БД. Приемлемо: они уже «известны», цель — дедуп.
- **Строки без ID** (если пользователь добавил вручную) — пропускаются; залогировать счётчик,
  чтобы было видно. Можно отдельным шагом генерировать им ID, но это вне scope.
- **Большая таблица** (сейчас ~590 строк) — `read_all` + один `SELECT` + батч `INSERT OR IGNORE`
  дёшево; на старте добавит <1s.
- **`normalize_url` менялся между версиями** — historically возможны несовпадения url_norm.
  На текущем коде один и тот же URL даёт один norm, так что в рамках одной версии безопасно.

---

## 10. Вне scope (на будущее, не в этом PR)

- Sheets-as-source-of-truth для дедупа в реальном времени (нужно только при нескольких
  одновременных окружениях — владелец выбрал одно).
- Уникальный индекс `UNIQUE(url_norm)` на стороне зеркала, чтобы `mirror_new_row` не дописывал
  дубль в саму таблицу.
- Hosted-БД (Postgres) — не требуется при одном окружении.
