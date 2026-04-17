# Сайт ihor.com — Архитектура и план

Публичное репо: `github.com/igrdevelop/ihor-dev`
Отдельно от бота (job-hunter — приватный).

---

## Что в итоге

```
ihor.com/          — Resume / Portfolio (Angular app)
ihor.com/hunt      — Job Hunt Dashboard (Angular app)
ihor.com/learn     — Interview Prep (Angular app)
ihor.com/api/...   — NestJS backend (REST API)
```

Всё на одном VPS (том же что и бот). nginx раздаёт трафик.

---

## Структура репо (Nx monorepo)

```
ihor-dev/
├── apps/
│   ├── resume/          ← Angular: твоё резюме и портфолио (ihor.com/)
│   ├── hunt/            ← Angular: дашборд охоты на работу (ihor.com/hunt)
│   ├── learn/           ← Angular: карточки для подготовки к интервью (ihor.com/learn)
│   └── api/             ← NestJS: backend (ihor.com/api/...)
├── libs/
│   ├── ui/              ← Общие компоненты (кнопки, карточки)
│   ├── models/          ← Общие TypeScript интерфейсы
│   └── utils/           ← Хелперы, используемые везде
├── nx.json
├── package.json
└── docker-compose.yml   ← Запуск всего стека
```

**Почему Nx?**
Один `npm install` — всё работает. Можно переиспользовать компоненты между apps.
Команды типа `nx build resume`, `nx test hunt`. На собеседованиях звучит хорошо.

**Почему NestJS?**
TypeScript как Angular, похожая архитектура (модули, декораторы, DI).
Лучше для твоего портфолио Angular-разработчика, чем FastAPI.

---

## Фазы

- [ ] Фаза 1 — Nx монорепо: создать, настроить, первый коммит
- [ ] Фаза 2 — Angular app "resume": базовая страница с CV
- [ ] Фаза 3 — NestJS app "api": первый endpoint
- [ ] Фаза 4 — nginx + Docker: всё в контейнерах, роутинг
- [ ] Фаза 5 — CI/CD: GitHub Actions → деплой на VPS
- [ ] Фаза 6 — Hunt Dashboard: UI для просмотра Applications
- [ ] Фаза 7 — Learn app: карточки для interview prep

---

## Фаза 1 — Nx монорепо

### Шаг 1.1 — Создать репо на GitHub

Зайди на github.com → New repository:
- Название: `ihor-dev`
- Visibility: **Public** (портфолио — должно быть видно)
- Без README (создадим сами)

### Шаг 1.2 — Создать Nx монорепо локально

Выбери папку где хочешь хранить (например `D:\LearningProject\`).
В PowerShell:

```powershell
cd D:\LearningProject
npx create-nx-workspace@latest ihor-dev --preset=apps --packageManager=npm
```

Когда спросит:
- Which CI provider? → **GitHub Actions**
- Enable distributed caching? → **No** (пока не нужно)

```powershell
cd ihor-dev
git remote add origin https://github.com/igrdevelop/ihor-dev.git
git push -u origin main
```

### Шаг 1.3 — Добавить Angular и NestJS плагины

```powershell
npx nx add @nx/angular
npx nx add @nx/nest
```

### Шаг 1.4 — Создать приложения

```powershell
# Angular apps
npx nx g @nx/angular:app resume --directory=apps/resume --style=scss --routing=true
npx nx g @nx/angular:app hunt --directory=apps/hunt --style=scss --routing=true
npx nx g @nx/angular:app learn --directory=apps/learn --style=scss --routing=true

# NestJS backend
npx nx g @nx/nest:app api --directory=apps/api

# Общая библиотека UI компонентов
npx nx g @nx/angular:lib ui --directory=libs/ui --publishable=false
```

### Шаг 1.5 — Проверить что всё работает

```powershell
# Запустить resume app
npx nx serve resume
# Открой http://localhost:4200

# Запустить api
npx nx serve api
# Открой http://localhost:3000
```

### Шаг 1.6 — Первый коммит

```powershell
git add .
git commit -m "chore: init Nx monorepo with Angular + NestJS apps"
git push
```

---

## Фаза 2 — Angular app "resume"

### Что делаем

Минимальная страница с твоим CV. Потом добавишь анимации, проекты, ссылки.

### Шаг 2.1 — Структура компонентов

```
apps/resume/src/app/
├── header/          ← Имя, контакты, ссылки
├── about/           ← Краткое описание
├── experience/      ← Список позиций
├── skills/          ← Технологии
└── footer/
```

Создать компоненты:

```powershell
cd apps/resume/src/app
npx nx g @nx/angular:component header --project=resume
npx nx g @nx/angular:component about --project=resume
npx nx g @nx/angular:component experience --project=resume
npx nx g @nx/angular:component skills --project=resume
```

### Шаг 2.2 — Собрать для продакшена

```powershell
npx nx build resume --configuration=production
# Результат в dist/apps/resume/
```

---

## Фаза 3 — NestJS app "api"

### Шаг 3.1 — Первый endpoint

Открой `apps/api/src/app/app.controller.ts`.

Добавь endpoint для проверки:

```typescript
@Get('health')
health() {
  return { status: 'ok', timestamp: new Date().toISOString() };
}
```

Проверь: `http://localhost:3000/api/health`

### Шаг 3.2 — Endpoints для Hunt Dashboard (позже)

```
GET  /api/applications        ← список всех заявок из tracker.xlsx
GET  /api/applications/stats  ← статистика (всего, SENT, FAIL)
POST /api/apply               ← запустить apply для URL (webhook от Telegram?)
```

### Шаг 3.3 — Собрать

```powershell
npx nx build api --configuration=production
# Результат в dist/apps/api/
```

---

## Фаза 4 — Docker + nginx

### Шаг 4.1 — Dockerfile для resume (Angular SPA)

Создай `apps/resume/Dockerfile`:

```dockerfile
# Этап 1: сборка
FROM node:20-alpine AS builder
WORKDIR /workspace
COPY package*.json ./
RUN npm ci
COPY . .
RUN npx nx build resume --configuration=production

# Этап 2: nginx для раздачи статики
FROM nginx:alpine
COPY --from=builder /workspace/dist/apps/resume/browser /usr/share/nginx/html
COPY apps/resume/nginx.conf /etc/nginx/conf.d/default.conf
EXPOSE 80
```

Создай `apps/resume/nginx.conf`:

```nginx
server {
    listen 80;
    root /usr/share/nginx/html;
    index index.html;

    # Angular routing — все пути отдают index.html
    location / {
        try_files $uri $uri/ /index.html;
    }
}
```

### Шаг 4.2 — Dockerfile для api (NestJS)

Создай `apps/api/Dockerfile`:

```dockerfile
FROM node:20-alpine AS builder
WORKDIR /workspace
COPY package*.json ./
RUN npm ci
COPY . .
RUN npx nx build api --configuration=production

FROM node:20-alpine
WORKDIR /app
COPY --from=builder /workspace/dist/apps/api .
COPY --from=builder /workspace/node_modules ./node_modules
EXPOSE 3000
CMD ["node", "main.js"]
```

### Шаг 4.3 — nginx как reverse proxy (главный)

Создай `nginx/nginx.conf` в корне монорепо:

```nginx
server {
    listen 80;
    server_name ihor.com www.ihor.com;

    # Portfolio / Resume — главная страница
    location / {
        proxy_pass http://resume:80;
        proxy_set_header Host $host;
    }

    # Hunt Dashboard
    location /hunt {
        proxy_pass http://hunt:80;
        proxy_set_header Host $host;
    }

    # Interview Prep
    location /learn {
        proxy_pass http://learn:80;
        proxy_set_header Host $host;
    }

    # NestJS API
    location /api/ {
        proxy_pass http://api:3000/;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

### Шаг 4.4 — docker-compose.yml (в корне монорепо)

```yaml
version: "3.9"

services:
  # Главный nginx (роутинг трафика)
  nginx:
    image: nginx:alpine
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./nginx/nginx.conf:/etc/nginx/conf.d/default.conf
    depends_on:
      - resume
      - hunt
      - learn
      - api
    restart: always

  # Angular Resume App
  resume:
    build:
      context: .
      dockerfile: apps/resume/Dockerfile
    restart: always

  # Angular Hunt Dashboard
  hunt:
    build:
      context: .
      dockerfile: apps/hunt/Dockerfile
    restart: always

  # Angular Interview Prep
  learn:
    build:
      context: .
      dockerfile: apps/learn/Dockerfile
    restart: always

  # NestJS API
  api:
    build:
      context: .
      dockerfile: apps/api/Dockerfile
    restart: always
    env_file:
      - .env
    volumes:
      # Читаем tracker.xlsx от бота (bot живёт в /home/deploy/job-hunter/)
      - /home/deploy/job-hunter/tracker.xlsx:/app/tracker.xlsx:ro

  logging:
    driver: "json-file"
    options:
      max-size: "10m"
      max-file: "3"
```

---

## Фаза 5 — CI/CD для сайта

### Шаг 5.1 — GitHub Secrets для этого репо

Зайди на github.com/igrdevelop/ihor-dev →
Settings → Secrets → Actions → New repository secret:

| Name | Value |
|------|-------|
| `VPS_HOST` | IP сервера (тот же что и бот) |
| `VPS_USER` | `deploy` |
| `VPS_SSH_KEY` | приватный SSH ключ |
| `VPS_WORK_DIR` | `/home/deploy/ihor-dev` |
| `GHCR_TOKEN` | GitHub token с правами packages |

### Шаг 5.2 — Workflow файл

Создай `.github/workflows/deploy.yml`:

```yaml
name: Deploy ihor-dev

on:
  push:
    branches: [ main ]

env:
  REGISTRY: ghcr.io

jobs:
  build-and-deploy:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Log in to Container Registry
        uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GHCR_TOKEN }}

      # Собрать и запушить все образы
      - name: Build and push resume
        uses: docker/build-push-action@v5
        with:
          context: .
          file: apps/resume/Dockerfile
          push: true
          tags: ghcr.io/${{ github.repository }}/resume:latest

      - name: Build and push api
        uses: docker/build-push-action@v5
        with:
          context: .
          file: apps/api/Dockerfile
          push: true
          tags: ghcr.io/${{ github.repository }}/api:latest

      # Деплой на VPS
      - name: Deploy to VPS
        uses: appleboy/ssh-action@v1.0.3
        with:
          host: ${{ secrets.VPS_HOST }}
          username: ${{ secrets.VPS_USER }}
          key: ${{ secrets.VPS_SSH_KEY }}
          script: |
            cd ${{ secrets.VPS_WORK_DIR }}
            echo ${{ secrets.GHCR_TOKEN }} | docker login ghcr.io -u ${{ github.actor }} --password-stdin
            docker-compose pull
            docker-compose up -d
            docker image prune -f
            echo "Deploy complete!"
```

---

## Фаза 6 — Hunt Dashboard

### Что это

Angular app на `ihor.com/hunt`:
- Таблица всех заявок (из tracker.xlsx через NestJS API)
- Статистика: сколько SENT, FAIL, сколько за неделю
- Возможно: кнопка "применить к вакансии" (POST /api/apply)

### Данные

NestJS читает `tracker.xlsx` (примонтирован как volume, только чтение).
Возвращает JSON → Angular отображает.

```
GET /api/applications
→ [{ company, title, date, status, url }, ...]

GET /api/applications/stats
→ { total: 54, sent: 48, failed: 6, thisWeek: 12 }
```

---

## Фаза 7 — Interview Prep (learn)

### Что это

Angular app на `ihor.com/learn`:
- Карточки с вопросами по Angular, TypeScript, RxJS
- Flip-карточки (вопрос / ответ)
- Прогресс (localStorage)

### Данные

Статические JSON файлы с вопросами — не нужен backend.

---

## На сервере — как будет выглядеть

```
/home/deploy/
├── job-hunter/          ← бот (из приватного репо)
│   ├── .env
│   ├── tracker.xlsx
│   ├── Applications/
│   └── docker-compose.yml
│
└── ihor-dev/            ← сайт (из публичного репо)
    ├── docker-compose.yml
    ├── nginx/
    │   └── nginx.conf
    └── .env (если нужен)
```

Два отдельных docker-compose стека.
Бот и сайт не мешают друг другу.

---

## Порядок работы (с чего начать)

1. Сначала — задеплоить бот (DEPLOY.md)
2. Когда бот работает на VPS → браться за сайт
3. Начинать с Фазы 1 (Nx монорепо) → Фазы 2 (resume app)
4. Фазы 3-7 — по мере надобности

**Сайт — это отдельный проект. Делать после того как бот на VPS.**

---

## Про домен ihor.com

Домен регистрируется отдельно (Cloudflare, Namecheap, etc.).
После регистрации:
- DNS запись `A`: `ihor.com → IP твоего VPS`
- DNS запись `A`: `www.ihor.com → IP твоего VPS`
- HTTPS: Let's Encrypt (certbot) — бесплатно, настраивается за 5 минут

```bash
# На VPS, после настройки nginx:
apt install certbot python3-certbot-nginx -y
certbot --nginx -d ihor.com -d www.ihor.com
```

Certbot сам настроит nginx на HTTPS и авторелевацию сертификата.
