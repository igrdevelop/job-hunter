# 02 — Lock-файл зависимостей + детерминизм деплоя

**Приоритет:** P0 · **Усилие:** часы · **Ветка:** `chore/deps-lockfile-deploy-pinning`

## Для чего апдейт

Три связанных проблемы воспроизводимости:

1. **Зависимости не запинены.** `requirements.txt` почти весь без версий
   (кроме google-* и ruff в CI). Docker-образ собирается заново на **каждый
   push в master** — любой breaking-release `python-telegram-bot`,
   `cloudscraper`, `anthropic` или `openai` молча въезжает в прод вместе с
   однострочным фиксом. Обнаружится это ночью, падением бота.
2. **Два источника зависимостей.** `requirements.txt` и
   `pyproject.toml [project.dependencies]` синхронизируются вручную (в
   requirements.txt даже живёт комментарий-напоминание об этом). Уже сейчас
   они расходятся: `openai`, `pytz`, `pypdf` есть в requirements, а
   `playwright` — только в optional-deps pyproject.
3. **Невоспроизводимый деплой.** `docker compose pull` тянет тег `latest`;
   sha-тег пушится в GHCR, но нигде не используется → отката «на вчерашний
   образ» одной командой нет.

Бонус-находка: `build-backend = "setuptools.backends.legacy:build"` в
pyproject.toml — на setuptools 65 (локальная машина) такого модуля **не
существует** (стандарт — `setuptools.build_meta`). Docker может работать за
счёт свежего setuptools в build isolation, но строка либо опечатка, либо
зависимость от недокументированного пути.

## Как именно будет происходить

1. Исправить build-backend на `setuptools.build_meta` (и убрать
   `backend` из requires, если не нужен) → проверить `pip install -e .`
   локально и сборку Docker-образа.
2. Сделать pyproject.toml **единственным** источником зависимостей:
   - перенести `openai`, `pytz`, `pypdf`, `pytest` в pyproject (main/dev);
   - сгенерировать lock: `uv pip compile pyproject.toml --all-extras -o requirements.lock`
     (uv быстрее; fallback — pip-tools `pip-compile`);
   - `requirements.txt` удалить, вместо него закоммитить `requirements.lock`
     с шапкой «generated — не редактировать руками».
3. `Dockerfile`: `pip install -r requirements.lock` → `pip install -e . --no-deps`
   (структура та же, меняется только файл).
4. CI (`deploy.yml`, job `test`): ставить из `requirements.lock` — тесты
   гоняются на тех же версиях, что едут в прод.
5. Деплой-детерминизм: в deploy-step на VPS экспортировать
   `IMAGE_TAG=${{ github.sha }}` и в `docker-compose.yml` заменить
   `image: ...:latest` на `image: ...:${IMAGE_TAG:-latest}`. Откат = задать
   старый sha и `docker compose up -d`.
6. Обновление зависимостей становится осознанным: раз в 2–4 недели
   `uv pip compile --upgrade` отдельным PR (позже можно навесить Renovate,
   но это опционально — объём зависимостей маленький).

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `pyproject.toml` | Фикс build-backend; полный список deps (в т.ч. openai/pytz/pypdf); pytest/ruff → `[project.optional-dependencies].dev` |
| `requirements.txt` → `requirements.lock` | Удалить ручной файл; закоммитить сгенерированный lock (с версиями и хешами) |
| `Dockerfile` | `-r requirements.txt` → `-r requirements.lock` |
| `.github/workflows/deploy.yml` | test-job ставит из lock; deploy-step передаёт `IMAGE_TAG=$GITHUB_SHA` в окружение compose |
| `docker-compose.yml` | `image: ghcr.io/...:${IMAGE_TAG:-latest}` |
| `CLAUDE.md` | Repository Layout + правило «новая зависимость → pyproject + перегенерировать lock» (вместо старого «add here AND in pyproject») |

## Критерий готовности

- Docker-образ собирается из lock; `pip check` в образе чистый.
- Два последовательных билда без изменения lock дают идентичный набор версий.
- Прогон отката на VPS: `IMAGE_TAG=<старый sha> docker compose up -d` поднимает
  предыдущую версию.

## Риски

- Первая генерация lock зафиксирует **текущие установленные в проде** версии?
  Нет — она зафиксирует свежие. Поэтому сразу после мержа смотреть первый
  прод-цикл (source_health, /status): если что-то сломалось от «свежих»
  версий — это ровно тот класс поломки, который lock впредь и предотвращает,
  чинится пином проблемного пакета.
- `playwright` ставится в Docker отдельно (`playwright install chromium`) —
  проверить, что optional-extra `browser` попал в lock (`--all-extras`).
