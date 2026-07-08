# LLM Cost Reduction Plan — снизить $/вакансию без потери качества

> Статус: утверждён владельцем 2026-07-08. Ветка: `feat/llm-cost-reduction`.
> Исполнитель: агент (Sonnet). Один milestone = один коммит с тестами.
> Язык плана — русский (для владельца), код/коммиты — английский.

## Контекст и диагноз

Реальные затраты ~$0.40–0.60 на вакансию (Sonnet, API mode). На Sonnet
**output-токены ($15/M) — доминирующая статья**; input почти весь через кэш.
Разбивка одного apply (см. `hunter/apply_api.py`):

| Этап | Вызовы | Доля затрат |
|---|---|---|
| Основная генерация (Step 3) | 1× Sonnet, output = 6 больших полей (resume EN+PL, CL EN+PL, about_me EN+PL) | ~30–40% |
| ATS-loop rewrites (`_ats_check_loop`) | 0–5× Sonnet | ~15–25% |
| CL review + PL перевод | 1–2× Sonnet | ~5% |
| Claim judge + verdict | 2× Haiku | ~3% |
| **Verdict refine loop** (`hunter/verdict_refine.py`) | до 3× Sonnet + 3× Haiku + PL-mirror, **откаченные раунды стоят столько же** | ~25–40% |

С `ATS_VERDICT_TARGET=95` при реальных вердиктах 72–94 refine-цикл срабатывает
почти на 100% генераций — это удвоение затрат, замеченное 2026-07-06 ($2→$6/день).

**Критерий качества** для всего плана: всё, что доставляется пользователю,
по-прежнему проходит те же ворота (scrubs → claim judge → language gate →
независимый verdict). Ни одно изменение не ослабляет ни один gate. Экономия
достигается тем, что мы перестаём (а) генерировать то, что не доставляется,
(б) повторять вызовы с тем же входом, (в) платить Sonnet-цену за механические
задачи.

Ожидаемый суммарный эффект M1–M5: **−40–50% на вакансию** (~$0.50 → ~$0.25–0.30).

---

## M1 — Refine loop: не повторять honest-раунд после отката

**Проблема.** `hunter/verdict_refine.py::refine_loop`: после rollback раунда
фидбек строится из того же `best_verdict`, а вход — тот же `best_content`.
Промпт следующего honest-раунда отличается ТОЛЬКО номером в заголовке
(`_HONEST_BLOCK.format(round=...)`). Одинаковый вход → почти одинаковый выход →
второй rollback. Это гарантированно выброшенный Sonnet-вызов (~$0.10) на
большинстве вакансий.

**Изменение** (только `refine_loop`, публичная сигнатура не меняется):
- Завести локальный флаг `escalate_after_rollback`. Когда раунд заканчивается
  rollback'ом (verdict не улучшился) И следующий раунд был бы honest с
  неизменившимся `best_verdict`:
  - если в оставшемся бюджете раундов возможен stretch (`max_rounds >= STRETCH_FROM_ROUND`
    не требуем — достаточно что раунды ещё остались): следующий раунд принудительно
    выполняется как `kind="stretch"` (независимо от `round_num`);
  - если stretch-раунд уже был выполнен и тоже откачен — остановить цикл
    (`break`), дальше улучшений не будет.
- Раунды, ЗАКОНЧИВШИЕСЯ принятием (verdict вырос), сбрасывают флаг: у следующего
  раунда новый `best_verdict` → новый фидбек → honest снова осмыслен.
- Раунды, отброшенные ДО verdict-а (role-drop, language-gate block, validation) —
  тоже считаются «вход не изменился» → эскалация, не повтор.
- Логи в существующем стиле: `[verdict_refine] round N: escalating to stretch
  after rollback (same input would repeat)`.

**Тесты** (`tests/test_verdict_refine.py`, дополнить):
1. Раунд 1 (honest) откачен → раунд 2 выполняется как stretch (проверить, что
   в rewrite-промпт ушёл `_STRETCH_BLOCK`).
2. Раунд 1 принят (score вырос) → раунд 2 остаётся honest (текущее поведение).
3. Honest откачен, затем stretch откачен → цикл останавливается (нет 3-го вызова).
4. `max_rounds=1` — поведение байт-в-байт прежнее (одна попытка, никаких эскалаций).
5. `stretch_additions` из эскалированного раунда попадают в `to_learn` (как раньше
   для kind=stretch).

**Acceptance:** экономия ~1 Sonnet-вызов на каждом «rollback-пути»; принятые
раунды не затронуты; `ATS_VERDICT_MAX_REFINES`/`ATS_VERDICT_TARGET` не трогаем.

---

## M2 — Инструменты замера: шум судьи + корреляция с воронкой (read-only)

**Проблема.** Target 95 при вердиктах 72–94 может оплачивать шум Haiku-оценщика,
а не качество. Прежде чем менять target (решение владельца, НЕ этого плана) —
нужны данные.

**Изменение 1:** новый `tools/verdict_noise.py`:
- CLI: `python tools/verdict_noise.py [--n 10] [--k 3] [--dir Applications]`.
- Берёт последние `--n` папок заявок, где есть EN CV PDF + `job_posting.txt`
  (переиспользовать извлечение текста из `hunter/ats_pdf_roundtrip.py` — там уже
  есть PDF→text; verdict через `run_llm_verdict` или напрямую
  `ats_checker.llm_verdict` с теми же caps 6000/9000).
- Прогоняет verdict `--k` раз на КАЖДОМ (одинаковый вход), печатает: per-folder
  min/max/spread, общий σ (population std по отклонениям от per-folder mean),
  и вывод вида: `Judge noise σ=X.X pp. A target within σ*2 of typical scores
  buys noise, not quality.`
- Стоимость прогона указать в help (~n*k Haiku-вызовов, ≈$0.01 каждый). Никаких
  записей в tracker/Sheets/content.json.

**Изменение 2:** новый `tools/verdict_funnel_corr.py`:
- Read-only по `tracker.db`: строки, где `ats_verdict` не NULL. Разбить на
  корзины (<80, 80–84, 85–89, 90–94, 95+), по каждой: count, sent-rate,
  confirmed-rate, answered-rate (логика колонок — как в `hunter/funnel.py`;
  переиспользовать её классификацию sent/confirmed/answered, не дублировать).
- Вывод — таблица + строка-вывод: различается ли answered-rate между корзинами.

**Тесты:** по 2–3 юнита на каждый tool (моки verdict-вызова; in-memory tracker.db
как в существующих tests/test_funnel.py). CLI-парсинг + агрегация.

**Acceptance:** оба инструмента работают на прод-данных без побочных эффектов.
В конце milestone — прогнать `verdict_funnel_corr.py` на локальном tracker.db
и вписать результат в PR description (verdict_noise требует API-ключ — только
если `ANTHROPIC_API_KEY` доступен, иначе отметить «запустить на прод-хосте»).

---

## M3 — Детерминированные ключевые слова в ПЕРВЫЙ генерационный промпт

**Проблема.** ATS-loop дорого чинит то, что можно было попросить сразу:
извлечение ключевых слов из постинга — регексы `hunter/ats_checker.py` ($0.00),
но первый генерационный вызов их не видит и первый resume закономерно
недобирает keyword-score → 1–2 лишних rewrite-раунда (~$0.08–0.16).

**Изменение** (`hunter/apply_api.py`, Step 3):
- Перед `call_llm` извлечь ключевые слова постинга детерминированно: в
  `ats_checker` найти/выделить функцию извлечения keywords из job_text (она уже
  есть внутри `check()` — если приватная, вынести чистую
  `extract_job_keywords(job_text) -> list[str]` без изменения поведения `check`).
- Прогнать список через существующий `_filter_self_description_keywords`
  (apply_shared) — те же слова, что ATS-loop считает «actionable».
- Cap ~30 штук. Если список пуст — блок не добавляется вовсе.
- Добавить в `user_message` блок:
  `\n\n## ATS keyword checklist (deterministic scan of this posting)\n`
  `Make sure EACH of these terms appears naturally in resume_en (skills and/or`
  ` experience bullets). Do not fabricate experience — place honestly:\n- kw1\n- kw2...`
- То же самое — в shadow-генерацию `hunter/dual_apply.py` (она переиспользует
  building blocks; проверить, строит ли она user_message сама — если да,
  добавить блок и туда, чтобы A/B оставался честным).
- CLI-pipeline (`apply_cli`) — НЕ трогать в этом milestone (другой механизм
  промпта; отметить как follow-up).

**Тесты:** блок появляется в user_message при непустом списке; отсутствует при
пустом; фильтр self-description применён; cap 30 работает; `extract_job_keywords`
даёт тот же список, что использует `check()` (регресс-тест на рефакторинг).

**Acceptance:** поведение ATS-loop не изменено (он остаётся страховкой);
ожидание — реже входим в rewrite-раунды. Замерить нечем в тестах — критерий
чисто структурный.

---

## M4 — Не генерировать PL-поля для англоязычных вакансий (PL по требованию)

**Проблема.** Главный вызов всегда генерирует resume_pl + cover_letter_pl +
about_me_pl — это ~40–50% output-токенов самого дорогого вызова. Для
EN-постинга (большинство remote-бордов) в short mode PL никуда не доставляется.

**Изменение** (API pipeline; CLI не трогаем):
- Новый config-ключ `GEN_SKIP_PL_FOR_EN` (default `true`, env-переключатель,
  секция рядом с остальными LLM-настройками; задокументировать в CLAUDE.md +
  `.env.example`).
- В `apply_api._run_main_api` вычислить `posting_lang = detect_posting_language(job_text)`
  РАНЬШЕ Step 3 (сейчас это Step 4.75 — перенести вычисление, само место gate
  не менять; переменная переиспользуется ниже, где она сейчас вычисляется).
- Если `posting_lang == "EN"` и флаг включён и НЕ `full_mode`: добавить в
  user_message инструкцию — `_pl`-поля вернуть пустыми строками
  (`"resume_pl": {}, "cover_letter_pl": "", "about_me_pl": ""`), генерировать
  только `_en`. Схему JSON не менять (ключи остаются — меньше поломок парсинга).
- **Совместимость по всей цепочке (главный риск milestone'а, пройти каждую точку):**
  - `validate_content` (apply_shared): убедиться, что пустые `_pl` не дают
    ошибок валидации, когда `_en` полные. Если валидатор требует `_pl` —
    ослабить ТОЛЬКО для случая «все `_pl` пустые одновременно» (частично
    пустые — по-прежнему ошибка).
  - `enforce_language_separation` / `lang_guard.scan_content`: пустые поля =
    чистые (проверить, не падает ли скан на пустом dict/строке).
  - `_ats_check_loop`: уже шлёт только resume_en — ок; guard восстановления
    ролей для resume_pl должен переносить пустоту без падения
    (`_orig_exp_pl == []`).
  - `generate_docs.py`: short mode для EN-постинга PL и так не рендерит;
    full mode — см. ниже. Проверить, что пустой resume_pl не рендерит пустой
    PDF и не падает.
  - `content_qa.run_qa`: не флагать пустые `_pl` как проблему при
    `primary_lang == "EN"` (если флагает — добавить условие).
  - claim judge `iter_judged_fields`: пустые поля просто не попадают в judged
    fields (проверить).
  - dual-apply shadow: получает тот же job_text → то же правило применяется
    само, если shadow строит промпт через общий код; если у shadow свой
    user_message — добавить ту же инструкцию.
- **PL по требованию:** если `full_mode == true` — генерировать как раньше
  (полный набор). Отдельного «дозаказа» PL для short-mode НЕ строить в этом
  milestone (YAGNI: PL-вакансии по-прежнему получают полный набор, у них
  `posting_lang == "PL"`).

**Тесты:** EN-постинг + флаг → инструкция в промпте есть, PL-постинг → нет,
full_mode → нет, флаг off → нет; validate_content принимает «все _pl пустые»;
lang gate/QA/judge не падают и не флагают на пустых _pl; регресс: PL-постинг
проходит весь пайплайн как раньше.

**Acceptance:** для EN-вакансий доставляемые файлы идентичны прежним
(EN CV/CL те же ворота); экономия ~40% output первого вызова.

---

## M5 — Переводы на дешёвую модель (Haiku)

**Проблема.** `_translate_resume` и `_translate_plain`
(`hunter/apply_shared.py`) гоняют механический PL↔EN перевод через основной
профиль (Sonnet, $15/M output). Перевод — задача уровня Haiku ($5/M), страховки
уже стоят: role-count guard + повторный скан lang-gate.

**Изменение:**
- Новые config-ключи: `TRANSLATE_PROVIDER` (default `anthropic`),
  `TRANSLATE_MODEL` (default = `JUDGE_MODEL`, т.е. Haiku),
  `TRANSLATE_API_KEY` (resolve-цепочка как у judge: `ANTHROPIC_API_KEY` →
  `LLM_API_KEY`). Если ключ не резолвится — fallback на основной профиль
  (`_llm_p()`), НЕ отказ от перевода.
- `_translate_resume` + `_translate_plain` используют translate-профиль.
  `_translate_cover_letter_pl` — уже wrapper, унаследует.
- PL-mirror в `verdict_refine.refine_loop` идёт через `_translate_resume` —
  унаследует автоматически.
- Учёт затрат: `_record_usage` уже пишет модель per-call, `llm_cost.PRICING`
  Haiku уже есть — ничего не менять.

**Тесты:** translate-вызовы уходят с judge/translate-моделью, а не с основной;
fallback на основной профиль при отсутствии ключа; существующие тесты
lang-gate зелёные (перевод мокается — проверить сигнатуры моков).

**Acceptance:** ×3 дешевле каждый перевод; качество страхуется существующими
guard'ами (role-count + повторный скан + block при выжившей контаминации).

---

## M6 — Агрегатор judge-находок (read-only, промпт-петля обратной связи)

**Проблема.** Судья и скрабы ловят одни и те же классы нарушений — каждое
пойманное = потраченный repair-вызов. Находки лежат в
`Applications/**/judge_report.json`, но в `generation_rules.md` не возвращаются.

**Изменение:** новый `tools/judge_stats.py`:
- Сканирует `Applications/**/judge_report.json` (+ `--dir` override),
  агрегирует violations по (severity, field-класс, нормализованный reason),
  печатает топ-20 с примерами quote и счётчиками, отдельно — доля fabrication
  vs exaggeration vs style.
- В конце — секция `## Suggested rule candidates`: для каждого частого класса
  одна строка-заготовка RED LINE (владелец сам решает, что вносить в
  `generation_rules.md` — инструмент правила НЕ пишет).

**Тесты:** агрегация на фикстурных judge_report.json (2–3 файла), нормализация
reason, пустая директория → чистый выход.

**Acceptance:** инструмент запущен на локальных данных, результат — в PR
description.

---

## Вне скоупа этого плана (решения владельца, зафиксировать в PR description)

1. **Снижение `ATS_VERDICT_TARGET` 95 → ~90** — только после данных M2.
2. **Смена боевого профиля на DeepSeek V3** — только после анализа накопленных
   dual-apply пар (shadow content.json на Drive/проде; вердикты сопоставимы —
   один и тот же Haiku-судья).
3. **CLI/Pro-режим** (`APPLY_USE_CLI=true`) — нулевая маржинальная цена
   генератора, но refine loop там пропускается.
4. Перенос keyword-checklist в CLI-pipeline (M3 follow-up).

## Общие требования

- Порядок: M1 → M2 → M3 → M4 → M5 → M6. Один milestone = один коммит
  (`feat(refine): ...`, `feat(tools): ...` и т.п.), тесты в том же коммите.
- После каждого milestone: `python -m compileall .`, `ruff check .`,
  `pytest tests/` — всё зелёное.
- CLAUDE.md: обновить config-таблицу (новые ключи M4/M5), Repository Layout
  (новые tools), Agent Work Log — одной записью в конце работы.
- `.env.example`: добавить новые ключи с комментариями.
- Ничего не менять в: `ATS_VERDICT_TARGET`/`ATS_VERDICT_MAX_REFINES` defaults,
  gates (judge/lang/doomed), tracker-схеме, Sheets-writers.
- PR в `master` в конце, с итогами прогонов M2/M6-инструментов в description.
