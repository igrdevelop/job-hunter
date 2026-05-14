# GDRIVE_PLAN.md — Google Drive Upload Integration

## Цель

После каждого успешного apply автоматически загружать папку с документами
(`Applications/{date}/{company}/`) на Google Drive. Пользователь получает ссылку
на папку в Telegram-уведомлении и может открыть документы с любого устройства.

---

## Контекст

- OAuth токен уже есть: `gsheets_token.json` запрошен со scope `drive.file`
  — отдельная авторизация **не нужна**
- `google-api-python-client` уже установлен (используется для Sheets)
- Паттерн клиент/синхронизация уже отработан на `gsheets_client.py` / `gsheets_sync.py`
- Хук после apply уже есть в `hunter/telegram_bot.py` (`_run_apply_agent`)

---

## Архитектура

```
apply_agent.py (subprocess)
    → generate_docs.py → Applications/{date}/{company}/
    → tracker_service.record_successful_apply()
    → [subprocess exit]

telegram_bot._run_apply_agent() [после subprocess success]:
    → cache.load_from_excel() + gsheets_sync.mirror_new_row()   ← уже есть
    → gdrive_sync.upload_application_folder(folder_path)         ← NEW
        → gdrive_client.get_or_create_folder("Job Hunter")
        → gdrive_client.get_or_create_folder("{date}", parent)
        → gdrive_client.get_or_create_folder("{company}", parent)
        → gdrive_client.upload_file(file, parent) × N files
        → returns folder_url
    → Telegram: добавляет ссылку Drive в уведомление
```

---

## Файлы для создания / изменения

### 1. `hunter/gdrive_client.py` (НОВЫЙ)

Низкоуровневый враппер Google Drive API v3.

```python
def build_service(credentials_file, token_file) -> Any
    # Строит Drive service из того же gsheets_token.json
    # Scopes: drive.file (уже есть в токене)

def get_or_create_folder(service, name: str, parent_id: str | None = None) -> str
    # Ищет папку с таким именем среди children parent_id
    # Если нет — создаёт. Возвращает folder_id.
    # Использует: files().list() + files().create()

def upload_file(service, file_path: Path, parent_id: str) -> str
    # Загружает файл, возвращает file_id
    # Использует: files().create(media_body=MediaFileUpload(...))
    # Если файл уже есть (по имени) — обновляет (files().update())

def upload_folder(service, folder_path: Path, parent_id: str) -> str
    # Загружает все файлы из папки (не рекурсивно — у нас плоская структура)
    # Возвращает folder_id папки на Drive

def folder_url(folder_id: str) -> str
    # "https://drive.google.com/drive/folders/{folder_id}"
```

### 2. `hunter/gdrive_sync.py` (НОВЫЙ)

Высокоуровневая логика синхронизации.

```python
# Константы (читаются из config)
# GDRIVE_ENABLED, GDRIVE_ROOT_FOLDER_ID

_service = None  # lazy singleton

def _get_service() -> Any | None
def _ready() -> bool

async def upload_application_folder(folder_path: Path) -> str | None
    """
    Загружает папку Applications/{date}/{company}/ на Drive.
    Структура на Drive: Job Hunter / {date} / {company} /
    Возвращает URL папки или None если GDRIVE_ENABLED=False или ошибка.
    """
    if not _ready():
        return None
    # 1. get_or_create_folder("Job Hunter") или GDRIVE_ROOT_FOLDER_ID
    # 2. get_or_create_folder(date_str, parent=root_id)
    # 3. upload_folder(folder_path, parent=date_folder_id)
    # 4. return folder_url(company_folder_id)
```

### 3. `hunter/config.py` (ИЗМЕНИТЬ)

Добавить в конец блока конфигурации:

```python
# ── Google Drive integration ──────────────────────────────────────────────────
GDRIVE_ENABLED: bool = os.getenv("GDRIVE_ENABLED", "false").lower() in ("true", "1", "yes")
# Опционально: ID корневой папки на Drive (если не задан — создаётся "Job Hunter" в корне)
GDRIVE_ROOT_FOLDER_ID: str = os.getenv("GDRIVE_ROOT_FOLDER_ID", "")
# Имя корневой папки (если GDRIVE_ROOT_FOLDER_ID не задан)
GDRIVE_ROOT_FOLDER_NAME: str = os.getenv("GDRIVE_ROOT_FOLDER_NAME", "Job Hunter")
```

### 4. `hunter/telegram_bot.py` (ИЗМЕНИТЬ)

В `_run_apply_agent()`, после блока gsheets mirror:

```python
# Upload application folder to Google Drive (best-effort)
if url and outcome != "fail":
    try:
        from hunter.tracker import get_folder_by_url
        from hunter.config import PROJECT_DIR, GDRIVE_ENABLED
        if GDRIVE_ENABLED:
            folder_str = await asyncio.to_thread(get_folder_by_url, url)
            if folder_str:
                from hunter import gdrive_sync
                drive_url = await gdrive_sync.upload_application_folder(
                    PROJECT_DIR / folder_str
                )
                if drive_url:
                    await _tg_notify(
                        f"📁 <a href=\"{drive_url}\">Открыть папку на Drive</a>"
                    )
    except Exception as _e:
        logger.warning("[apply_agent] gdrive upload failed: %s", _e)
```

### 5. `tests/test_gdrive_client.py` (НОВЫЙ)

Тесты с mock — аналогично `test_gsheets_client.py`:
- `test_get_or_create_folder_creates_when_missing`
- `test_get_or_create_folder_reuses_existing`
- `test_upload_file_calls_create`
- `test_upload_folder_uploads_all_files`

### 6. `tests/test_gdrive_sync.py` (НОВЫЙ)

- `test_upload_application_folder_noop_when_disabled`
- `test_upload_application_folder_creates_structure`
- `test_upload_application_folder_returns_none_on_error`

---

## Конфигурация (.env)

```env
GDRIVE_ENABLED=true
# Опционально — если хочешь класть в конкретную папку на Drive:
# GDRIVE_ROOT_FOLDER_ID=<folder_id_from_drive_url>
```

---

## Структура папок на Google Drive

```
My Drive/
└── Job Hunter/                          ← создаётся автоматически
    ├── 2026-05-14/
    │   ├── Acme/
    │   │   ├── CV_EN.pdf
    │   │   ├── Cover_Letter_EN.pdf
    │   │   └── job_posting.txt
    │   └── TechCorp/
    │       └── ...
    └── 2026-05-15/
        └── ...
```

---

## Порядок реализации для агента

1. Создать `hunter/gdrive_client.py` с 4 функциями
2. Создать `hunter/gdrive_sync.py` с lazy singleton + `upload_application_folder()`
3. Добавить GDRIVE_* в `hunter/config.py`
4. Добавить импорты GDRIVE_ENABLED в `hunter/telegram_bot.py` и хук после apply
5. Написать тесты (mock, без сети)
6. `python -m pytest tests/` — все проходят
7. `python -m compileall .` — нет ошибок
8. Коммит: `feat: Google Drive upload after apply`
9. Обновить CLAUDE.md (добавить gdrive в архитектуру, конфиг таблицу, правила)
10. Финальный коммит с документацией

---

## Важные детали

- **Scope `drive.file`**: токен уже имеет этот scope. Новая авторизация не нужна.
  Но `drive.file` даёт доступ только к файлам созданным этим приложением.
  Это значит нельзя читать чужие папки, но создавать и загружать — можно.

- **Дубликаты**: если apply запущен повторно (`--force`), папка уже существует.
  `get_or_create_folder` должен переиспользовать существующую, а `upload_file`
  обновлять файлы если они уже есть (по имени).

- **Best-effort**: ошибка загрузки на Drive не должна ломать flow.
  Всё в try/except, логируется warning.

- **Большие файлы**: DOCX + PDF обычно < 5 MB — MediaFileUpload без resumable upload.

- **Папка `/app/Applications/`**: на сервере бот пишет в `/app/Applications/`.
  `get_folder_by_url()` возвращает относительный путь (`Applications/{date}/{company}`).
  Нужно комбинировать с `PROJECT_DIR` из config.

---

## Статус

- [ ] `hunter/gdrive_client.py`
- [ ] `hunter/gdrive_sync.py`
- [ ] `hunter/config.py` — GDRIVE_* vars
- [ ] `hunter/telegram_bot.py` — хук после apply
- [ ] `tests/test_gdrive_client.py`
- [ ] `tests/test_gdrive_sync.py`
- [ ] CLAUDE.md обновлён
