# Job Hunter — Deployment Plan V2

**Цель:** бот крутится на VPS 24/7, файлы зеркалируются на Google Drive,
обновление кода = push в master → CI/CD → автоперезапуск. В будущем — Angular сайт поверх.

---

## Статус

- [x] Фаза 0 — GitHub репо готов, develop → master структура
- [ ] Фаза 1 — Dockerfile исправлен для продакшна
- [ ] Фаза 2 — VPS на Hetzner
- [ ] Фаза 3 — Google Drive интеграция
- [ ] Фаза 4 — CI/CD пайплайн (GitHub Actions → GHCR → VPS)
- [ ] Фаза 5 — Первый живой деплой
- [ ] Фаза 6 — Сайт (Angular + FastAPI) [будущее]

---

## Фаза 1 — Dockerfile для продакшна

### 1.1 — Исправить Dockerfile

Текущий черновик в DEPLOY.md неполный. Продакшн-версия:

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# LibreOffice нужен для generate_docs.py (DOCX → PDF конвертация)
# gcc нужен для некоторых Python пакетов
RUN apt-get update && apt-get install -y \
    gcc \
    libreoffice \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p Applications backups

CMD ["python", "hunter.py"]
```

### 1.2 — Добавить Google API пакеты в requirements.txt

```
google-api-python-client==2.131.0
google-auth==2.29.0
google-auth-oauthlib==1.2.0
google-auth-httplib2==0.2.0
```

### 1.3 — Создать .dockerignore

```
.env
.secrets/
.git/
.github/
__pycache__/
*.pyc
*.pyo
tracker.xlsx
to_send.xlsx
Applications/
backups/
*.pdf
*.docx
.claude/
gmail_token.json
drive_token.json
gmail_credentials.json
drive_credentials.json
```

### 1.4 — Создать docker-compose.yml

```yaml
version: "3.9"

services:
  job-hunter:
    image: ghcr.io/igrdevelop/job-hunter:latest
    container_name: job-hunter
    restart: always
    env_file:
      - .env
    volumes:
      # Персистентные данные — живут на диске сервера
      - ./tracker.xlsx:/app/tracker.xlsx
      - ./to_send.xlsx:/app/to_send.xlsx
      - ./Applications:/app/Applications
      - ./backups:/app/backups
      - ./.secrets:/app/.secrets
      # Google токены — никогда не в образе
      - ./gmail_credentials.json:/app/gmail_credentials.json
      - ./gmail_token.json:/app/gmail_token.json
      - ./drive_credentials.json:/app/drive_credentials.json
      - ./drive_token.json:/app/drive_token.json
    logging:
      driver: "json-file"
      options:
        max-size: "10m"
        max-file: "5"
```

### 1.5 — Переменные окружения для сервера

Добавить в `.env` на сервере (не коммитить!):

```
# Существующие переменные — те же что локально
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
LLM_API_KEY=...
LLM_PROVIDER=anthropic
LLM_MODEL=claude-3-5-haiku-20241022

# Отключить на сервере
INHIRE_ENABLED=false
APPLY_USE_CLI=false

# Google Drive (новые)
GDRIVE_ENABLED=true
GDRIVE_FOLDER_ID=...        # ID корневой папки JobHunter на Drive
GDRIVE_CREDENTIALS=drive_credentials.json
GDRIVE_TOKEN=drive_token.json
GDRIVE_SYNC_TRACKER=true    # загружать tracker.xlsx на Drive
```

### 1.6 — Закоммитить

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "chore: production Dockerfile with LibreOffice + Drive deps"
git push origin develop
```

---

## Фаза 2 — VPS на Hetzner

### 2.1 — SSH ключ (если нет)

```powershell
ssh-keygen -t ed25519 -C "job-hunter-vps"
cat ~/.ssh/id_ed25519.pub   # скопировать — нужен при создании сервера
```

### 2.2 — Создать сервер

1. console.hetzner.com → New Project → "job-hunter"
2. Add Server:
   - Location: **Nuremberg**
   - Image: **Ubuntu 22.04**
   - Type: **Shared vCPU → x86 → CX22** (2 CPU, 4 GB RAM, €4.35/мес)
   - SSH Keys → Add → вставить публичный ключ из 2.1
   - Name: `job-hunter`
3. Create & Buy → **записать IP адрес**

### 2.3 — Подключиться и настроить

```bash
ssh root@ТВОЙ_IP

# Обновить систему
apt update && apt upgrade -y

# Установить Docker
curl -fsSL https://get.docker.com | sh
apt install docker-compose-plugin -y

# Создать пользователя deploy
useradd -m -s /bin/bash deploy
usermod -aG docker deploy
mkdir -p /home/deploy/job-hunter
chown deploy:deploy /home/deploy/job-hunter

# Скопировать SSH ключ для deploy
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

### 2.4 — Подключить Hetzner Volume (бэкапы)

В панели Hetzner → Volumes → Create Volume:
- Size: **10 GB** (~€0.50/мес)
- Location: Nuremberg (тот же что сервер)
- Name: `job-hunter-backups`
- Attach to сервер `job-hunter`

На сервере:

```bash
# Отформатировать и смонтировать
mkfs.ext4 /dev/disk/by-id/scsi-0HC_Volume_XXXXX   # ID из панели Hetzner
mkdir -p /mnt/backups
mount /dev/disk/by-id/scsi-0HC_Volume_XXXXX /mnt/backups
chown deploy:deploy /mnt/backups

# Автомонтирование при перезагрузке
echo "/dev/disk/by-id/scsi-0HC_Volume_XXXXX /mnt/backups ext4 defaults 0 0" >> /etc/fstab
```

Обновить `docker-compose.yml` — добавить volume для бэкапов с Volume:

```yaml
volumes:
  - /mnt/backups:/app/backups   # Hetzner Volume вместо локальной папки
```

### 2.5 — Загрузить файлы на сервер

С твоего компьютера (PowerShell):

```powershell
$VPS = "deploy@ТВОЙ_IP"
$SRC = "D:\LearningProject\Claude"
$DST = "/home/deploy/job-hunter"

# Секреты и данные
scp "$SRC\.env"                  "${VPS}:${DST}/"
scp "$SRC\tracker.xlsx"          "${VPS}:${DST}/"
scp "$SRC\gmail_credentials.json" "${VPS}:${DST}/"
scp "$SRC\gmail_token.json"      "${VPS}:${DST}/"
scp "$SRC\drive_credentials.json" "${VPS}:${DST}/"   # после Фазы 3
scp "$SRC\drive_token.json"      "${VPS}:${DST}/"    # после Фазы 3
scp -r "$SRC\.secrets"           "${VPS}:${DST}/"

# Создать папки
ssh $VPS "mkdir -p ${DST}/Applications ${DST}/backups"
```

---

## Фаза 3 — Google Drive интеграция

### 3.1 — Создать отдельный OAuth клиент для Drive

Drive использует **отдельные credentials** (не трогаем Gmail OAuth):

1. Зайти на console.cloud.google.com → открыть существующий проект (тот же где Gmail)
2. APIs & Services → Enable APIs → найти **Google Drive API** → Enable
3. APIs & Services → Credentials → **Create Credentials → OAuth client ID**
   - Application type: **Desktop app**
   - Name: `job-hunter-drive`
4. Download JSON → сохранить как `drive_credentials.json` в корне проекта
5. **Никогда не коммитить** (уже в .gitignore)

### 3.2 — Авторизовать Drive (локально, один раз)

Создать `tools/drive_auth.py`:

```python
"""Authorize Google Drive OAuth and save drive_token.json. Run once locally."""
from pathlib import Path
from google_auth_oauthlib.flow import InstalledAppFlow
from google.oauth2.credentials import Credentials
import json

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CREDS_FILE = Path("drive_credentials.json")
TOKEN_FILE = Path("drive_token.json")

flow = InstalledAppFlow.from_client_secrets_file(str(CREDS_FILE), SCOPES)
creds = flow.run_local_server(port=0)
TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
print(f"Saved: {TOKEN_FILE}")
```

Запустить локально:

```bash
python tools/drive_auth.py
# Откроется браузер → войти в Google аккаунт → разрешить доступ
# Сохранится drive_token.json
```

### 3.3 — Создать папку JobHunter на Drive

1. Открыть drive.google.com
2. Создать папку `JobHunter` → внутри создать `Applications` и `Tracker`
3. Открыть папку `JobHunter` → скопировать ID из URL:
   `https://drive.google.com/drive/folders/`**`1ABC123xyz`**
4. Записать этот ID → вставить в `.env` как `GDRIVE_FOLDER_ID=1ABC123xyz`

### 3.4 — Создать hunter/drive_client.py

```python
"""
hunter/drive_client.py — Google Drive uploader.

Uploads application folders (PDF/DOCX) and optionally tracker.xlsx to Drive.
Uses a separate OAuth token from Gmail (drive_token.json / drive_credentials.json).
"""

import logging
from pathlib import Path

from hunter.config import PROJECT_DIR, GDRIVE_ENABLED

logger = logging.getLogger(__name__)

_CREDS_FILE = PROJECT_DIR / "drive_credentials.json"
_TOKEN_FILE = PROJECT_DIR / "drive_token.json"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]


def _get_service():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        _TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return build("drive", "v3", credentials=creds)


def _get_or_create_folder(service, name: str, parent_id: str) -> str:
    """Return folder ID, creating it if it doesn't exist."""
    q = (
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed=false"
    )
    results = service.files().list(q=q, fields="files(id)").execute()
    files = results.get("files", [])
    if files:
        return files[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id],
    }
    folder = service.files().create(body=meta, fields="id").execute()
    return folder["id"]


def upload_application_folder(local_folder: Path, root_folder_id: str) -> str | None:
    """Upload all PDF/DOCX/TXT files from local_folder to Drive.

    Creates JobHunter/Applications/{date}/{company}/ structure on Drive.
    Returns the Drive folder URL, or None on failure.
    """
    if not GDRIVE_ENABLED:
        return None
    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_service()

        # local_folder is Applications/{date}/{company}/
        # Drive path: root_folder_id / Applications / {date} / {company}
        parts = local_folder.parts
        app_idx = next(i for i, p in enumerate(parts) if p == "Applications")
        date_part = parts[app_idx + 1]
        company_part = parts[app_idx + 2]

        apps_id = _get_or_create_folder(service, "Applications", root_folder_id)
        date_id = _get_or_create_folder(service, date_part, apps_id)
        company_id = _get_or_create_folder(service, company_part, date_id)

        UPLOAD_EXTS = {".pdf", ".docx", ".txt"}
        for f in sorted(local_folder.iterdir()):
            if f.suffix.lower() not in UPLOAD_EXTS:
                continue
            mime = (
                "application/pdf" if f.suffix == ".pdf"
                else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                if f.suffix == ".docx"
                else "text/plain"
            )
            media = MediaFileUpload(str(f), mimetype=mime, resumable=False)
            service.files().create(
                body={"name": f.name, "parents": [company_id]},
                media_body=media,
                fields="id",
            ).execute()
            logger.info("[drive] Uploaded: %s", f.name)

        url = f"https://drive.google.com/drive/folders/{company_id}"
        logger.info("[drive] Folder: %s", url)
        return url

    except Exception as e:
        logger.error("[drive] Upload failed: %s", e)
        return None


def upload_tracker(tracker_path: Path, root_folder_id: str) -> None:
    """Upload tracker.xlsx to JobHunter/Tracker/ on Drive."""
    if not GDRIVE_ENABLED:
        return
    try:
        from googleapiclient.http import MediaFileUpload

        service = _get_service()
        tracker_id = _get_or_create_folder(service, "Tracker", root_folder_id)

        mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        media = MediaFileUpload(str(tracker_path), mimetype=mime, resumable=False)

        # Overwrite existing file if it exists
        q = f"name='tracker.xlsx' and '{tracker_id}' in parents and trashed=false"
        existing = service.files().list(q=q, fields="files(id)").execute().get("files", [])
        if existing:
            service.files().update(
                fileId=existing[0]["id"], media_body=media
            ).execute()
        else:
            service.files().create(
                body={"name": "tracker.xlsx", "parents": [tracker_id]},
                media_body=media,
                fields="id",
            ).execute()
        logger.info("[drive] tracker.xlsx synced")
    except Exception as e:
        logger.error("[drive] tracker sync failed: %s", e)
```

### 3.5 — Добавить GDRIVE_ENABLED и GDRIVE_FOLDER_ID в config.py

```python
# Google Drive
GDRIVE_ENABLED: bool = os.getenv("GDRIVE_ENABLED", "false").lower() in ("true", "1", "yes")
GDRIVE_FOLDER_ID: str = os.getenv("GDRIVE_FOLDER_ID", "")
```

### 3.6 — Подключить Drive upload в apply_agent.py

В конце успешного apply (после generate_docs, перед Telegram отправкой):

```python
# Google Drive upload
drive_url = None
if GDRIVE_ENABLED and GDRIVE_FOLDER_ID:
    from hunter.drive_client import upload_application_folder, upload_tracker
    drive_url = upload_application_folder(Path(output_folder), GDRIVE_FOLDER_ID)
    if GDRIVE_SYNC_TRACKER:
        upload_tracker(TRACKER_PATH, GDRIVE_FOLDER_ID)
```

Включить Drive folder URL в Telegram-сообщение:

```python
if drive_url:
    msg += f"\n📁 <a href='{drive_url}'>Drive папка</a>"
```

### 3.7 — Ночная досинхронизация (cron через JobQueue)

Новый файл `tools/sync_to_drive.py` — сканирует `Applications/` и загружает всё что ещё не на Drive (по отсутствию файла с тем же именем в папке на Drive). Запускается как scheduled task в telegram_bot.py раз в сутки в 03:00.

---

## Фаза 4 — CI/CD пайплайн

### 4.1 — GitHub Secrets

Зайти: github.com/igrdevelop/job-hunter → Settings → Secrets → Actions

| Secret | Значение |
|--------|----------|
| `VPS_HOST` | IP сервера |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | содержимое `~/.ssh/id_ed25519` (приватный ключ) |
| `VPS_WORK_DIR` | `/home/deploy/job-hunter` |
| `GHCR_TOKEN` | GitHub PAT (write:packages, read:packages) |
| `TELEGRAM_BOT_TOKEN` | токен бота (для алертов на сбой) |
| `TELEGRAM_CHAT_ID` | твой chat ID |

### 4.2 — GitHub Container Registry токен

GitHub профиль → Settings → Developer settings → Personal access tokens → Tokens (classic):
- Note: `job-hunter-ghcr`
- Expiration: No expiration
- Scopes: `write:packages`, `read:packages`, `delete:packages`
- Сохранить как секрет `GHCR_TOKEN`

### 4.3 — Workflow файл

Создать `.github/workflows/deploy.yml`:

```yaml
name: Deploy Job Hunter

on:
  push:
    branches: [ master ]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"
      - name: Install dependencies
        run: pip install -r requirements.txt
      - name: Run tests
        run: pytest tests/ -q

  build-and-deploy:
    needs: test
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Log in to GHCR
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      - name: Build and push image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ env.IMAGE_NAME }}:latest
            ghcr.io/${{ env.IMAGE_NAME }}:${{ github.sha }}

      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ${{ secrets.VPS_WORK_DIR }}
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
            docker compose pull
            docker compose up -d
            docker image prune -f
            echo "Deploy complete"

      - name: Notify on failure
        if: failure()
        uses: appleboy/telegram-action@master
        with:
          to: ${{ secrets.TELEGRAM_CHAT_ID }}
          token: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          message: |
            ❌ Job Hunter deploy FAILED
            Branch: ${{ github.ref_name }}
            Commit: ${{ github.sha }}
            https://github.com/${{ github.repository }}/actions/runs/${{ github.run_id }}
```

### 4.4 — Закоммитить

```bash
git add .github/ Dockerfile .dockerignore docker-compose.yml requirements.txt hunter/drive_client.py hunter/config.py tools/drive_auth.py
git commit -m "feat: CI/CD pipeline + Drive integration"
git push origin develop
```

---

## Фаза 5 — Первый живой деплой

### 5.1 — Залогиниться в GHCR на сервере (один раз)

```bash
ssh deploy@ТВОЙ_IP
cd /home/deploy/job-hunter
echo ТУТ_GHCR_TOKEN | docker login ghcr.io -u igrdevelop --password-stdin
```

### 5.2 — Смержить в master → запустить CI/CD

```bash
# Локально
git checkout master
git merge develop
git push origin master
```

Открыть github.com/igrdevelop/job-hunter → вкладка **Actions** — ждём зелёную галочку (~3-5 мин).

### 5.3 — Проверить

```bash
ssh deploy@ТВОЙ_IP
cd /home/deploy/job-hunter
docker compose ps             # статус: Up
docker compose logs -f        # логи в реальном времени
```

В Telegram: `/start` → должен ответить. `/hunt` → должны прийти вакансии.

### 5.4 — Проверить Drive

После первого apply через бот:
- Telegram прислал PDF и ссылку на Drive папку
- На Drive появилась `JobHunter/Applications/{date}/{company}/` с файлами
- `JobHunter/Tracker/tracker.xlsx` обновился

---

## Рабочий процесс после деплоя

```
Разработка локально (develop ветка)
  → git push origin develop
  → PR или прямой merge в master
  → git push origin master
  → GitHub Actions: тесты → Docker образ → деплой на VPS (~4 мин)
  → Бот перезапустился с новым кодом
```

Никакого ручного SSH после первого деплоя.

---

## Фаза 6 — Сайт (Angular + FastAPI) [будущее]

### Концепция

```
Angular SPA (frontend)
    ↕ HTTP API
FastAPI (backend) ← читает tracker.xlsx, Applications/
    ↕ shared volume
Job Hunter Bot (уже работает)
```

### Что нужно будет сделать

**6.1 — Домен и HTTPS**
- Проверить DNS: привязать домен к IP сервера (A-запись)
- Установить Nginx + Certbot:
  ```bash
  apt install nginx certbot python3-certbot-nginx -y
  certbot --nginx -d твой-домен.com
  ```

**6.2 — FastAPI бэкенд (`website/api/`)**
- `GET /api/applications` — список всех применений из tracker.xlsx
- `GET /api/applications/{id}/files` — список файлов в папке
- `GET /api/applications/{id}/files/{filename}` — скачать файл
- `POST /api/hunt` — запустить hunt вручную (прокси к боту)
- Auth: простой Bearer токен в `.env`

**6.3 — Angular фронтенд (`website/frontend/`)**
- Таблица вакансий с фильтрами (статус, дата, стек)
- Предпросмотр PDF прямо в браузере
- Кнопки Apply / Skip / Force
- Дашборд: статистика по источникам, по стеку, по дням

**6.4 — docker-compose.yml расширенный**
```yaml
services:
  job-hunter:        # уже есть
    ...
  api:
    build: ./website/api
    volumes:
      - ./tracker.xlsx:/app/tracker.xlsx:ro
      - ./Applications:/app/Applications:ro
  frontend:
    build: ./website/frontend
    ports:
      - "80:80"
      - "443:443"
```

**6.5 — Nginx конфиг**
```nginx
server {
    server_name твой-домен.com;
    location /api/ { proxy_pass http://api:8000/; }
    location /     { root /usr/share/nginx/html; try_files $uri /index.html; }
}
```

---

## Справочник команд на сервере

```bash
# Логи
docker compose logs -f job-hunter

# Перезапустить без обновления образа
docker compose restart job-hunter

# Ручное обновление (обычно делает CI/CD)
docker compose pull && docker compose up -d

# Зайти внутрь контейнера
docker exec -it job-hunter bash

# Ресурсы
docker stats job-hunter

# Статус
docker compose ps
```

---

## Прогресс лог

| Дата | Кто | Что |
|------|-----|-----|
| 2026-05-13 | sonnet-4-6 | План создан на основе ответов на 11 вопросов |
