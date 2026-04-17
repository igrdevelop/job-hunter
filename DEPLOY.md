# Деплой Job Hunter на VPS

Пошаговый гайд — делаешь сам, строчка за строчкой.

---

## Где мы сейчас

- [x] Фаза 1 — GitHub репо создан, код запушен
- [ ] Фаза 2 — Dockerfile
- [ ] Фаза 3 — VPS на Hetzner
- [ ] Фаза 4 — GitHub Actions CI/CD
- [ ] Фаза 5 — Первый запуск

---

## Фаза 2 — Dockerfile (делаешь локально)

### Что такое Dockerfile?

Dockerfile — это рецепт. Ты описываешь как собрать "коробку" (контейнер) с твоим ботом.
Docker читает этот файл и создаёт образ — снапшот всего что нужно для запуска:
Python, библиотеки, код. Потом этот образ запускается на любом сервере одинаково.

---

### Шаг 2.1 — Создать Dockerfile

Открой VS Code в папке `D:\LearningProject\Claude`.
Создай файл `Dockerfile` (без расширения) в корне проекта.

Содержимое:

```dockerfile
# Берём официальный образ Python 3.11
# slim = облегчённая версия без лишних инструментов (меньше размер)
FROM python:3.11-slim

# Устанавливаем рабочую папку внутри контейнера
# Все следующие команды будут выполняться отсюда
WORKDIR /app

# Устанавливаем gcc — нужен для компиляции некоторых Python пакетов
# После установки чистим кеш apt чтобы не раздувать образ
RUN apt-get update && apt-get install -y \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Копируем ТОЛЬКО requirements.txt (не весь код)
# Зачем отдельно? Docker кеширует слои.
# Если код изменился но requirements.txt нет — pip install не запустится снова
COPY requirements.txt .

# Устанавливаем зависимости
# --no-cache-dir = не сохранять кеш pip (экономим место)
RUN pip install --no-cache-dir -r requirements.txt

# Теперь копируем весь код
# Это отдельный слой — при изменении кода пересобирается только он
COPY . .

# Создаём папку для документов (будет перекрыта volume с сервера)
RUN mkdir -p Applications

# Команда запуска бота
CMD ["python", "hunter.py"]
```

После создания файла — скажи "готово", идём дальше.

---

### Шаг 2.2 — Создать .dockerignore

Это как .gitignore, только для Docker.
Docker не будет копировать эти файлы/папки в образ.

Создай файл `.dockerignore` в корне проекта:

```
# Секреты — никогда не должны попасть в образ
.env
.secrets/

# Git
.git/
.github/

# Python кеш
__pycache__/
*.pyc
*.pyo

# Персональные данные — не нужны в образе
tracker.xlsx
Applications/

# Документы
*.pdf
*.docx

# Claude Code
.claude/

# Тесты и dev файлы
*.md
DEPLOY.md
```

После создания файла — скажи "готово".

---

### Шаг 2.3 — Создать docker-compose.yml

docker-compose — это способ описать как запускать контейнер на сервере:
какие переменные окружения, какие папки пробросить, политика перезапуска.

Создай файл `docker-compose.yml` в корне проекта:

```yaml
version: "3.9"

services:
  job-hunter:
    # Берём образ из GitHub Container Registry (туда будет пушить CI/CD)
    image: ghcr.io/igrdevelop/job-hunter:latest
    container_name: job-hunter

    # always = если контейнер упал или сервер перезагрузился — автозапуск
    restart: always

    # Загружаем переменные окружения из .env файла на сервере
    env_file:
      - .env

    # Volumes — пробрасываем папки с сервера внутрь контейнера
    # Формат: путь_на_сервере:путь_внутри_контейнера
    volumes:
      # tracker.xlsx живёт на диске сервера, не теряется при обновлении образа
      - ./tracker.xlsx:/app/tracker.xlsx
      # Сгенерированные документы тоже сохраняются
      - ./Applications:/app/Applications
      # LinkedIn сессия (если используется)
      - ./.secrets:/app/.secrets

    # Настройки логов
    logging:
      driver: "json-file"
      options:
        max-size: "10m"   # максимум 10MB на файл лога
        max-file: "3"     # хранить последние 3 файла
```

После создания файла — скажи "готово".

---

### Шаг 2.4 — Закоммитить и запушить

После того как все три файла созданы, выполни в терминале:

```bash
git add Dockerfile .dockerignore docker-compose.yml
git commit -m "chore: add Docker configuration"
git push
```

---

### Проверка Фазы 2

После пуша зайди на github.com/igrdevelop/job-hunter —
в репо должны появиться три новых файла:
- `Dockerfile`
- `.dockerignore`
- `docker-compose.yml`

---

## Фаза 3 — VPS на Hetzner

### Что такое VPS?

VPS (Virtual Private Server) — это твой компьютер в интернете.
Работает 24/7, имеет публичный IP адрес, ты управляешь им через SSH.

Hetzner — немецкий провайдер, лучшее соотношение цена/качество в Европе.
CX22 — 2 CPU, 4GB RAM, 40GB SSD — €4.35/мес (~18 PLN).
Этого хватит для бота + сайта в будущем.

---

### Шаг 3.1 — Создать SSH ключ (если нет)

SSH ключ — это как пароль, только безопаснее. Состоит из двух частей:
- приватный ключ (у тебя на компьютере, никому не давать)
- публичный ключ (даёшь серверу — он тебя узнаёт)

Открой PowerShell и выполни:

```powershell
ssh-keygen -t ed25519 -C "job-hunter-vps"
```

Когда спросит путь — нажми Enter (сохранит в ~/.ssh/id_ed25519).
Когда спросит passphrase — можно оставить пустым (просто Enter).

Покажи публичный ключ:

```powershell
cat ~/.ssh/id_ed25519.pub
```

Скопируй весь вывод — он понадобится при создании сервера.

---

### Шаг 3.2 — Создать сервер на Hetzner

1. Зайди на console.hetzner.com → зарегистрируйся
2. New Project → название "job-hunter"
3. Add Server:
   - Location: **Nuremberg** (ближе к Польше)
   - Image: **Ubuntu 22.04**
   - Type: **Shared vCPU → x86 → CX22**
   - SSH Keys → Add SSH Key → вставь публичный ключ из шага 3.1
   - Name: `job-hunter`
4. Create & Buy

Запиши IP адрес сервера — он появится на странице после создания.

---

### Шаг 3.3 — Первый вход на сервер

```powershell
ssh root@ТВОЙ_IP
```

Если спросит "Are you sure?" — введи `yes`.

Ты внутри сервера. Командная строка изменится на что-то вроде:
```
root@job-hunter:~#
```

---

### Шаг 3.4 — Установить Docker

Выполни команды по очереди:

```bash
# Обновить список пакетов
apt update && apt upgrade -y

# Установить Docker одной командой (официальный скрипт)
curl -fsSL https://get.docker.com | sh

# Установить docker-compose
apt install docker-compose -y

# Проверить что всё установилось
docker --version
docker-compose --version
```

---

### Шаг 3.5 — Создать пользователя deploy

Работать под root — плохая практика. Создаём отдельного пользователя:

```bash
# Создать пользователя
useradd -m -s /bin/bash deploy

# Добавить в группу docker (чтобы мог запускать контейнеры)
usermod -aG docker deploy

# Создать папку для проекта
mkdir -p /home/deploy/job-hunter
chown deploy:deploy /home/deploy/job-hunter
```

Добавить SSH ключ для пользователя deploy:

```bash
mkdir -p /home/deploy/.ssh
cp ~/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
chmod 700 /home/deploy/.ssh
chmod 600 /home/deploy/.ssh/authorized_keys
```

Проверь что можешь войти как deploy (с твоего компьютера, не с сервера):

```powershell
ssh deploy@ТВОЙ_IP
```

---

### Шаг 3.6 — Загрузить файлы на сервер

С твоего компьютера (не с сервера!), в PowerShell:

```powershell
# Загрузить .env с секретами
scp D:\LearningProject\Claude\.env deploy@ТВОЙ_IP:/home/deploy/job-hunter/

# Загрузить docker-compose.yml
scp D:\LearningProject\Claude\docker-compose.yml deploy@ТВОЙ_IP:/home/deploy/job-hunter/

# Загрузить tracker.xlsx
scp D:\LearningProject\Claude\tracker.xlsx deploy@ТВОЙ_IP:/home/deploy/job-hunter/

# Создать папку Applications на сервере
ssh deploy@ТВОЙ_IP "mkdir -p /home/deploy/job-hunter/Applications"

# Загрузить .secrets если используешь LinkedIn
scp -r D:\LearningProject\Claude\.secrets deploy@ТВОЙ_IP:/home/deploy/job-hunter/
```

---

## Фаза 4 — GitHub Actions CI/CD

### Что такое GitHub Actions?

Это автоматизация внутри GitHub. Ты описываешь в YAML файле:
"когда кто-то пушит в master — сделай вот это".

"Вот это" в нашем случае:
1. Собери Docker образ из кода
2. Запушь образ в GitHub Container Registry
3. Зайди на VPS по SSH
4. Скачай новый образ
5. Перезапусти контейнер

---

### Шаг 4.1 — Добавить секреты в GitHub

Зайди на github.com/igrdevelop/job-hunter →
Settings → Secrets and variables → Actions → New repository secret

Добавь по одному:

| Name | Value |
|------|-------|
| `VPS_HOST` | IP твоего сервера (например 65.21.123.45) |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | содержимое файла `~/.ssh/id_ed25519` (приватный ключ!) |
| `VPS_WORK_DIR` | `/home/deploy/job-hunter` |

Показать содержимое приватного ключа:
```powershell
cat ~/.ssh/id_ed25519
```

Копируй ВСЁ включая `-----BEGIN...` и `-----END...`.

---

### Шаг 4.2 — GitHub Container Registry токен

GitHub → Settings (твой профиль, не репо!) →
Developer settings → Personal access tokens → Tokens (classic) →
Generate new token (classic)

- Note: `job-hunter-ghcr`
- Expiration: No expiration
- Поставь галочки: `write:packages`, `read:packages`, `delete:packages`
- Generate token → скопируй токен

Добавь как секрет в репо (шаг 4.1):

| Name | Value |
|------|-------|
| `GHCR_TOKEN` | токен который только что скопировал |

---

### Шаг 4.3 — Создать workflow файл

Создай папки и файл:
```
.github/
  workflows/
    deploy.yml
```

Содержимое `deploy.yml`:

```yaml
name: Deploy Job Hunter

# Запускать при каждом push в ветку master
on:
  push:
    branches: [ master ]

env:
  REGISTRY: ghcr.io
  IMAGE_NAME: ${{ github.repository }}

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest

    steps:
      # 1. Скачать код из репозитория
      - name: Checkout code
        uses: actions/checkout@v4

      # 2. Залогиниться в GitHub Container Registry
      - name: Log in to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ${{ env.REGISTRY }}
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      # 3. Собрать и запушить Docker образ
      - name: Build and push Docker image
        uses: docker/build-push-action@v5
        with:
          context: .
          push: true
          tags: |
            ghcr.io/${{ env.IMAGE_NAME }}:latest
            ghcr.io/${{ env.IMAGE_NAME }}:${{ github.sha }}

      # 4. Задеплоить на VPS через SSH
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ${{ secrets.VPS_WORK_DIR }}

            # Залогиниться в GHCR
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin

            # Скачать новый образ
            docker pull ghcr.io/${{ env.IMAGE_NAME }}:latest

            # Перезапустить контейнер с новым образом
            docker-compose up -d --pull always

            # Удалить старые образы (экономия диска)
            docker image prune -f

            echo "✅ Deploy complete!"
```

После создания файла:

```bash
git add .github/
git commit -m "chore: add GitHub Actions CI/CD pipeline"
git push origin develop
```

---

### Шаг 4.4 — Сделать первый деплой

Сейчас CI/CD запускается при push в **master**.
Смержи develop в master:

```bash
git checkout master
git merge develop
git push origin master
```

Зайди на github.com/igrdevelop/job-hunter → вкладка **Actions**.
Увидишь запущенный workflow. Он займёт 2-3 минуты.
Зелёная галочка = деплой прошёл успешно.

---

## Фаза 5 — Первый запуск

### Шаг 5.1 — Зайти на сервер

```powershell
ssh deploy@ТВОЙ_IP
cd /home/deploy/job-hunter
```

### Шаг 5.2 — Залогиниться в GHCR (один раз)

```bash
echo ТУТ_ТВОЙ_GHCR_TOKEN | docker login ghcr.io -u igrdevelop --password-stdin
```

### Шаг 5.3 — Запустить бота

```bash
docker-compose up -d
```

### Шаг 5.4 — Проверить что работает

```bash
# Статус контейнера
docker-compose ps

# Логи в реальном времени (Ctrl+C чтобы выйти)
docker-compose logs -f
```

Если в Telegram пришло сообщение от бота — всё работает.

---

## Полезные команды (на сервере)

```bash
# Смотреть логи
docker-compose logs -f job-hunter

# Перезапустить
docker-compose restart job-hunter

# Остановить
docker-compose stop

# Обновить вручную (обычно это делает CI/CD автоматически)
docker-compose pull && docker-compose up -d

# Зайти внутрь контейнера для отладки
docker exec -it job-hunter bash

# Сколько памяти/CPU использует контейнер
docker stats job-hunter
```

---

## После деплоя — как обновлять бота

Теперь рабочий процесс:

```bash
# 1. Сделай изменения в коде локально
# 2. Закоммить
git add .
git commit -m "fix: что-то исправил"

# 3. Смержи в master
git checkout master
git merge develop
git push origin master

# 4. GitHub Actions автоматически задеплоит за 2-3 минуты
# Следи в github.com/igrdevelop/job-hunter → Actions
```

Ничего больше делать не нужно.
