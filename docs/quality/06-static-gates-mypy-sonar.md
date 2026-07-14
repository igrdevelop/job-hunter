# 06 — Статические гейты: оживить mypy и SonarCloud

**Приоритет:** P2 · **Усилие:** часы на подключение + фоновая доводка · **Ветка:** `ci/mypy-and-sonar`

## Для чего апдейт

Два гейта существуют «декоративно»:

1. **mypy сконфигурирован, но не запускается нигде.** В `pyproject.toml` есть
   `[tool.mypy]` (py3.11, ignore_missing_imports, exclude tests/tools), но ни
   CI-job, ни pre-commit его не вызывают. Мёртвый конфиг создаёт ложное
   ощущение типизации. При 62k строк и активной генерации кода агентами типы —
   самый дешёвый ловец класса «переименовали поле dict / передали str вместо
   Path / вернули None в путь, где ждут список».
2. **SonarCloud-job написан, но выключен** — скипает себя, пока в секретах
   нет `SONAR_TOKEN`. Для публичного репо бесплатен; даст duplication-детектор
   (подсветит зеркала из дока 05), code smells и coverage-на-новый-код в PR
   (отчёт из дока 04).

## Как именно будет происходить

### mypy — поэтапное ужесточение, без остановки работы

1. **Этап 0**: CI-job `mypy hunter/ llm_client.py generate_docs.py apply_agent.py`
   с `continue-on-error: true` — только видимость, ничего не блокирует.
   Зафиксировать текущее число ошибок (baseline) в описании PR.
2. **Этап 1**: выправить дешёвые массовые ошибки (Optional-поля dataclass,
   `dict[str, Any]` на content, отсутствующие return-аннотации в core-модулях:
   `tracker.py`, `filters.py`, `models.py`, `config.py`).
3. **Этап 2**: снять `continue-on-error` → mypy становится гейтом на текущем
   (уже нулевом) уровне ошибок при дефолтных настройках.
4. **Этап 3 (фоновый, опционально)**: `disallow_untyped_defs = true`
   помодульно через `[[tool.mypy.overrides]]`, начиная с новых файлов
   (`pipeline/` из дока 05 сразу пишется типизированным).
   `--strict` целиком — НЕ цель.

Альтернатива, если mypy окажется слишком шумным на этом коде: pyright в
basic-режиме. Решение по факту baseline-а этапа 0.

### SonarCloud — 15 минут

1. Подключить репо на sonarcloud.io (аккаунт GitHub владельца), получить токен.
2. `SONAR_TOKEN` в GitHub Secrets — job в `deploy.yml` уже написан и
   развернётся сам (informational, деплой от него не зависит — так и оставить).
3. В `sonar-project.properties` добавить `sonar.python.coverage.reportPaths=coverage.xml`
   (после дока 04).
4. Первый скан: разобрать top-findings; правила-шум (дублирующие ruff)
   отключить в Sonar-профиле, а не игнорить в коде.

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `.github/workflows/deploy.yml` | Новый job `typecheck` (mypy, сначала continue-on-error); test-job передаёт coverage.xml артефактом в sonar-job |
| `pyproject.toml` | `mypy` в dev extras; `[[tool.mypy.overrides]]` для помодульного ужесточения; удалить exclude tools/ из основной секции только если tools реально проходят |
| `hunter/*.py` (core) | Точечные аннотации/фиксы под этап 1 — без изменения поведения |
| `sonar-project.properties` | coverage path + при необходимости `sonar.exclusions` для fixtures |
| `CLAUDE.md` | «Important Rules»: mypy добавляется к ruff в списке пред-коммитных проверок |

## Критерий готовности

- CI: mypy-job зелёный и блокирующий (этап 2 достигнут).
- SonarCloud badge/PR-декорация работает; quality gate настроен на «new code»
  (не на весь легаси разом).
- В `pyproject.toml` не осталось конфигов, которые ничем не исполняются.

## Риски

- mypy на коде с 292 `except Exception` и dict-центричным content.json может
  дать сотни ошибок → поэтому этап 0 не блокирует, а ужесточение помодульное.
  Если baseline > ~400 ошибок — сузить скоуп этапа 2 до `hunter/` без
  `sources/` (скрейперы типизировать последними, у них самый динамичный код).
- Sonar может задублировать ruff-findings — глушить на стороне Sonar-профиля,
  чтобы в коде оставался один источник ignore-правил (pyproject).
