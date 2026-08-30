# Промпт для Sonnet — Resume Profile Store (реализация)

Ты работаешь над Job Hunter Bot. Полный план — в
`docs/RESUME_PROFILE_STORE_PLAN.md`, прочитай его ПЕРВЫМ. Этот файл — рабочий
порядок: что делать, маленькими шагами, один шаг = один коммит.

## Контекст (что будет сделано, коротко)

Сегодня данные кандидата живут в трёх руками написанных файлах
(`users/{uid}/candidate/`): `candidate.yaml` (структурированные факты,
читаются через `hunter/candidate.py::get(dotpath)`), `candidate_profile.md`
(свободный текст карьеры для LLM) и `base_cv_<track>.md` (буллеты по стеку).
Новый пользователь не может их написать — онбординг мёртв.

Строим: **структурированный профиль (JSON, ядро + трековые варианты) как
источник правды** → пользователь загружает своё резюме на сайте → парсер
раскладывает его в профиль → пользователь правит/дополняет поля в редакторе
(сайт, другой репозиторий) → детерминированный **рендер профиля в те же три
файла**. Apply-пайплайн, фильтры, judge, `/tracks` — НЕ меняются вообще: они
продолжают читать те же файлы.

В этом репозитории делаем три вещи: **схему**, **рендерер**, **парсер** (+ CLI
для API). Редактор/загрузка/хранение ревизий — в репозиториях api/site, не здесь.

**Важно (wave 2, PR #235, в master с 2026-08-30):** часть структуры уже
существует. `employers.history[]` в candidate.yaml — структурированная история
ролей (`company/title/period` + опциональные `backend`, `bullets_max`,
`legacy_stack_ok`, `title_by_track`), и новый модуль `hunter/gen_prompt.py`
рендерит её в генерационный/судейский промпты на лету. Наши `core.roles` — это
history-запись ПЛЮС `description` и `bullets`; рендерер обязан проецировать
`employers.history` из roles в том же виде. Прочитай `hunter/gen_prompt.py`,
`candidate/candidate.yaml.example` (секции employers/experience) и
`tests/test_gen_prompt.py` до начала работы.

## Предусловия (проверь перед стартом)

- M0b и M0c из плана пройдены (решение владельца записано в план). Если в
  плане `Status: draft` без отметки о прохождении M0 — остановись и спроси.
- Ветка срезана от актуального `origin/master` (сначала `git fetch origin`).

## Железные правила (нарушение = переделка)

1. **Никаких персональных данных владельца** в коде, фикстурах и
   `profile.example.json` — только нейтральные плейсхолдеры ("Jane Doe").
   `tests/test_handoff_readiness.py` это проверяет и уронит CI.
2. **Никаких новых зависимостей.** Схема и валидация — на stdlib
   (`dataclasses`, `json`). Если что-то очень нужно — сначала спроси
   (новая зависимость = правка `pyproject.toml` + регенерация
   `requirements.lock`, см. CLAUDE.md).
3. **Не трогай пайплайн**: `apply_api.py`, `apply_cli.py`, `generate_docs.py`,
   `hunter/candidate.py`, `hunter/filters.py`, `hunter/gen_prompt.py`,
   `prompts/generation_rules.md`, `prompts/judge_rules.md` и т.д. — читать
   можно, менять нельзя.
4. После каждого шага: `ruff check .` + `ruff format .` + `pytest tests/` —
   всё зелёное до коммита. Сообщения коммитов — на английском.
5. LLM-вызов есть только в парсере (шаг 3c), уровень `JUDGE_MODEL` (Haiku).
   Больше нигде LLM не добавлять.
6. CLAUDE.md обновляется в том же коммите, где меняется поведение/появляется
   файл, который в нём должен быть описан (Repository Layout).

---

## Шаг 1a — модели схемы (`hunter/profile_schema.py`)

Один новый файл + тесты. Ничего не потребляет его — риск нулевой.

- Dataclasses по скетчу из плана (раздел "Shared contract"): `Profile`,
  `Core`, `Identity`, `Location`, `Languages`, `Employers`, `FlexibleEmployer`,
  `Education`, `Experience`, `Role`, `Bullet`, `Skill`, `Extra`, `Variant`,
  `Leftover`, `Upload`. Поле `schema_version: int = 1` на корне.
- `Role` = запись wave-2 `employers.history` + наши поля: `company`, `title`,
  `period`, опциональные `backend`, `bullets_max`, `legacy_stack_ok`,
  `title_by_track: dict[str, str]` (сверь имена и типы с
  `candidate/candidate.yaml.example` и `hunter/gen_prompt.py` — они уже в
  проде), плюс `subtitle`, `description`, `stack_line`, `bullets: list[Bullet]`
  (общий нарратив-суперсет для candidate_profile.md) и пер-трековые
  override-поля НА УРОВНЕ РОЛИ: `bullets_by_track: dict[str, list[str]]`
  (полная замена списка буллетов для base_cv этого трека — в реальных файлах
  владельца это ПЕРЕПИСАННЫЕ буллеты с другим фреймингом и другим
  количеством, не отфильтрованные), `subtitle_by_track`, `stack_line_by_track`.
  Скилы: `list[SkillCategory]` (`category` + `items: list[str]`) в core, и
  полный собственный список у каждого варианта (у react-трека свои ярлыки
  вроде "Angular (background)" — это не переупорядочивание).
- `Experience`: `years_label: str`, `since_year: int` (wave 2).
- `Core.generation_notes: str = ""` — необязательный свободный текст
  («story bank», см. `candidate/generation_rules.local.example.md`).
- `origin` у Role/Bullet/Skill/Extra: литерал `"parsed" | "edited"`.
- `tracks: list[str]` у Bullet/Skill; пустой список = элемент общий для всех
  треков.
- `from_dict(data: dict) -> Profile` — толерантный конструктор: неизвестные
  ключи игнорирует с `logger.warning`, отсутствующие — дефолтит; НИКОГДА не
  бросает исключение на кривых данных, вместо этого собирает список проблем.
- `validate(profile) -> list[str]` — список человекочитаемых проблем
  (пустой = валиден). Обязательные поля для рендера: `identity.full_name`,
  `identity.contact`, `identity.cv_filename_prefix` (те же, что в
  `candidate.require_identity()` — `REQUIRED_IDENTITY_FIELDS`).
- `to_dict(profile) -> dict` — сериализация, round-trip стабильный
  (`from_dict(to_dict(p))` эквивалентен `p`).

**Тесты** (`tests/test_profile_schema.py`): round-trip; неизвестный ключ не
роняет; validate ловит пустой full_name; пустой dict → валидный пустой профиль
с непустым списком проблем.

## Шаг 1b — пример профиля + тест покрытия дотпасов

- `candidate/profile.example.json` (трекается) — нейтральный, но ЗАПОЛНЕННЫЙ
  пример: 2 роли (по 2–3 буллета, у одного буллета тег трека), 6–8 скилов,
  1 вариант (`angular`), 1 leftover, комментировать негде (JSON) — поэтому
  рядом раздел в `candidate/README.md` (3–5 предложений, что это).
- `tests/test_profile_example.py`: пример загружается `from_dict`, `validate`
  пуст; **каждый из 24 дотпасов из плана (раздел M0a) выводим из примера** —
  захардкодь список дотпасов в тесте и проверь, что рендер-источник для каждого
  существует (пока просто наличие соответствующих полей в `Profile`).

## Шаг 2a — рендер `candidate.yaml`

`hunter/profile_render.py`, функция `render_candidate_yaml(profile) -> str`.

- Выводит YAML, покрывающий ВСЕ дотпасы из M0a (их 24). Ключевая логика —
  **производные поля**: `employers.real_companies` = lowercase от
  `protected + [flexible.name]`; `employers.profile_titles` = нормализованные
  уникальные `roles[].title`; `employers.history` = проекция `roles[]`
  (company/title/period + опциональные backend/bullets_max/legacy_stack_ok/
  title_by_track, БЕЗ description и bullets) — имена ключей ровно как в
  `candidate.yaml.example`, их читает боевой `hunter/gen_prompt.py`;
  `employers.protected` по умолчанию = все компании ролей минус
  `flexible.name`. В профиле производных полей НЕТ — они существуют только
  в рендере (это убирает ручную синхронизацию копий).
- `tracks.base_cv` = `{track: f"base_cv_{track}.md" for track in variants}`.
- Не забудь `experience.{years_label,since_year}` — их читает gen_prompt.
- Используй `yaml.safe_dump` (pyyaml уже в зависимостях), `sort_keys=False`,
  стабильный порядок секций как в `candidate.yaml.example`.

**Тесты** (`tests/test_profile_render.py`): рендер `profile.example.json` →
`yaml.safe_load` → каждый из 24 дотпасов резолвится (переиспользуй функцию
`resolves` из `tests/test_handoff_readiness.py` как образец); рендер записанный
во временный файл проходит `candidate.require_identity()`
(через `candidate._set_path()` / `path=`, как в `tests/test_candidate_multiuser.py`);
детерминизм — два рендера байт-в-байт равны; **совместимость с wave 2** —
скорми отрендеренный yaml рендереру employment-facts из `hunter/gen_prompt.py`
(посмотри, как это делает `tests/test_gen_prompt.py`) и проверь, что вышла
настоящая таблица фактов с компаниями из профиля, а НЕ его generic-абзац
деградации «нет истории».

## Шаг 2b — рендер `candidate_profile.md`

`render_profile_md(profile) -> str` в том же модуле.

- Порядок: headline + summary → роли (заголовок `Company — Title (Period)`,
  абзац description, ВСЕ буллеты маркдаун-списком — это суперсет, треки тут
  НЕ фильтруются) → education → языки → extras.
- Никакой генерации текста — только конкатенация того, что есть в профиле.
  Пустые секции пропускаются молча.

**Тесты**: голден-снапшот рендера примера
(`tests/fixtures/profile_render/candidate_profile.golden.md`, нейтральные
данные); пустой профиль рендерится без исключений в почти пустую строку.

## Шаг 2c — рендер `base_cv_<track>.md` + запись на диск

- `render_base_cv(profile, track) -> str`: буллеты и скилы фильтруются по
  треку (без тега = входит; с тегом = входит только если track в тегах),
  headline/summary/skills_order берутся из `variants[track]` с фолбэком на core.
- `render_all(profile, out_dir: Path) -> list[Path]` — пишет
  `candidate.yaml`, `candidate_profile.md`, по `base_cv_<track>.md` на каждый
  ключ `variants`, и — только если `generation_notes` непустой —
  `generation_rules.local.md` (текст как есть; формат см.
  `candidate/generation_rules.local.example.md`); возвращает записанные пути.
  Полная перезапись файлов, никаких merge.

**Тесты**: буллет с тегом `react` отсутствует в angular-рендере и присутствует
в react; профиль без variants пишет только два файла; `render_all` в tmp_path
→ набор файлов совпадает с ожидаемым.

После этого шага прогони skill `mutation-verify` на логике производных полей
(сломай lowercase в `real_companies` → тест должен упасть осмысленно).

## Шаг 3a — извлечение текста из docx/pdf

`hunter/profile_parse.py`, функция `extract_resume_text(path: Path) -> str`.

- `.docx` — через `python-docx` (параграфы + таблицы, разделитель `\n`).
- `.pdf` — переиспользуй существующую экстракцию из
  `hunter/ats_pdf_roundtrip.py` (посмотри, какая функция там читает текст из
  PDF, и вызови её; НЕ копируй код).
- `.txt`/`.md` — просто читаем.
- Неизвестное расширение / нечитаемый файл → `ProfileParseError` с понятным
  сообщением.

**Тесты** (`tests/test_profile_parse.py` + `tests/fixtures/resumes/`):
сгенерируй в тесте маленький docx через python-docx (не клади бинарники в
фикстуры без нужды), txt-фикстура с фейковым резюме ("Jane Doe…").

## Шаг 3b — каркас парсера без LLM

- `parse_resume_text(text: str, llm=None) -> Profile`:
  детерминированные пре-филлы — email/телефон через
  `hunter/contact_extract.py` (посмотри его API), имя НЕ угадываем.
- Без `llm` (None): весь текст уходит одним элементом в `leftovers`,
  контакты — в `identity.contact`. Это и есть фолбэк-ветка «парс никогда не
  падает жёстко».
- Все созданные элементы получают `origin="parsed"`.

**Тесты**: без llm текст оказывается в leftovers; email из текста попадает в
contact; результат проходит `from_dict(to_dict(...))`.

## Шаг 3c — LLM-вызов парсера

- Промпт — новый трекаемый файл `prompts/resume_parse.md`: инструкция
  «текст резюме → JSON по схеме» + правила (не выдумывать факты; что не
  удалось отнести — в `leftovers`; уровни владения скилами НЕ писать).
- Вызов через `llm_client.call_llm` с моделью/провайдером `JUDGE_MODEL` /
  `JUDGE_PROVIDER` (как это делает `hunter/prescreen.py` — возьми его за
  образец: один дешёвый вызов, толерантный разбор ответа).
- Ответ → `from_dict` → `validate`. Любая ошибка вызова/разбора/валидации →
  лог warning + фолбэк из шага 3b (весь текст в leftovers). Исключение наружу
  не выходит.

**Тесты**: через паттерн `fake_llm` из `tests/conftest.py` — валидный ответ
→ роли/скилы на месте с `origin="parsed"`; мусорный ответ → фолбэк-ветка;
исключение из call_llm → фолбэк-ветка.

## Шаг 4a — CLI для API

- `tools/parse_resume.py`: `python tools/parse_resume.py <file>` → профиль
  JSON в stdout, exit 0; ошибка → stderr + exit 1. Флаг `--no-llm` для
  дешёвого прогона.
- `tools/render_profile.py`: `python tools/render_profile.py <profile.json>
  <out_dir>` → пишет три+ файла, печатает JSON `{"written": [...]}`.
- Оба — тонкие обёртки над функциями шагов 2–3, argparse, без логики.
  Прецедент такого CLI-шва уже есть: `python -m hunter.gen_prompt` (его
  дёргает CLI-скилл `.claude/commands/apply.md`) — посмотри его `__main__`
  как образец интерфейса.

**Тесты**: subprocess-прогон обоих CLI на фикстурах (см. как гоняются другие
tools-скрипты в tests/, если прецедента нет — `subprocess.run([sys.executable, ...])`).

## Шаг 4b — документация

- CLAUDE.md: строки в Repository Layout про `hunter/profile_schema.py`,
  `hunter/profile_render.py`, `hunter/profile_parse.py`,
  `tools/parse_resume.py`, `tools/render_profile.py`,
  `candidate/profile.example.json`, `prompts/resume_parse.md`; запись в Agent
  Work Log.
- `docs/RESUME_PROFILE_STORE_PLAN.md`: Status → in progress/shipped по факту.
- `candidate/README.md` и `prompts/README.md` — по одному абзацу.

## Вне твоей зоны (НЕ делать)

- Шаг M5 плана (миграция владельца на VPS) — делает владелец руками.
- Редактор на сайте, эндпоинты API, хранение ревизий — другие репозитории.
- Любые изменения в apply-пайплайне, фильтрах, generate_docs.

## Definition of done (весь заказ)

`pytest tests/` зелёный (включая оба golden E2E), `ruff check` + `ruff format
--check` чистые, mypy-бейзлайн не вырос (223/217 — сверь актуальное число в
CLAUDE.md), `tests/test_handoff_readiness.py` зелёный, и цепочка работает
руками: `python tools/parse_resume.py tests/fixtures/resumes/jane.txt --no-llm
> /tmp/p.json && python tools/render_profile.py /tmp/p.json /tmp/out` даёт
файлы, на которых `candidate.get("identity.full_name", path=...)` возвращает
данные из профиля.
