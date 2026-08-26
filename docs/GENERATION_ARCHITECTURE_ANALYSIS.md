# Анализ: декомпозиция генерации и её настраиваемость

**Дата:** 2026-08-26 · **Тип:** анализ (не план — решений не принимает, M0 не содержит)
**Область:** всё, что происходит между «есть URL вакансии» и «на диске лежит PDF».
**Данные:** только код. Локальные `tracker.db` / `Applications/` в этом воркtree —
пустая заглушка (см. CLAUDE.md), поэтому все числа ниже либо померены по коду,
либо процитированы из `docs/AGENT_LOG.md`. Вопросы, требующие живых данных,
собраны в §7.

---

## 1. Резюме в пяти пунктах

1. **Пайплайн генерации существует в трёх рукописных копиях** — `apply_api.py`
   (1204), `apply_cli.py` (953), `dual_apply.py` (593). Это давно известно
   (`docs/quality/05-unify-apply-pipeline.md`), но с момента написания того
   плана копии **выросли**, а не сжались: 1039→1204, 776→953, 516→593,
   `apply_shared.py` 1879→2211.
2. **Копии разошлись содержательно, а не только по форме.** Пять стадий
   выполняются только на API-ветке. По `AGENT_LOG` 2026-08-24 через `main_cli`
   проходит ~14 из 60 недавних запусков (~23%) — значит на этой доле прогонов
   не работают content-QA, проверка bogus-company, compliance-скраб,
   ATS-keyword-loop и до-LLM стек-чеки.
3. **~394 строки `apply_shared.py` мертвы в проде** — подсистема self-review
   cover letter, удалённая из пайплайнов 2026-06-03 (PR #70). Она до сих пор
   описана в CLAUDE.md как «Pipeline Flow, Step 5», экспортируется из
   `apply_agent.py` и покрыта 18+ тестами, которые дают ложную уверенность.
4. **`prompts/generation_rules.md` — главный блокер настраиваемости.** Файл
   **в git**, 274 строки, из них 41 содержит личные факты владельца: таблица
   7 работодателей с точными периодами, «10+ years since 2015», ВУЗ, список
   курсов, правила «какой backend был у SII/Venture Labs». Это прямо
   противоречит правилу CLAUDE.md «личные факты — только через
   `hunter/candidate.py`» и галочке «✅ working tree is clean of personal data»
   в `docs/PUBLIC_RELEASE_CHECKLIST.md`. `tests/test_handoff_readiness.py` его
   не видит: он сканирует только `*.py`.
5. **Мультиюзерность сегодня — только про личность, не про политику.**
   `hunter/users.py::user_env()` прокидывает в дочерний процесс ровно три
   переменных (`CANDIDATE_YAML_PATH`, `APPLICATIONS_DIR`, `JOB_HUNTER_USER_ID`).
   Все поведенческие ручки генерации (`JUDGE_MODE`, `ATS_VERDICT_TARGET`,
   `ATS_VERDICT_MAX_REFINES`, `PRESCREEN_MODE`, `DOOMED_GATE_HARD_ACTION`,
   `CV_GDPR_CLAUSE`, `GEN_SKIP_PL_FOR_EN`) — глобальные env, одинаковые для всех.

---

## 2. Карта размеров (померено)

| Файл | строк | что это |
|---|---:|---|
| `hunter/apply_shared.py` | 2211 (53 функции) | «общая свалка»: гейты, скрабы, язык, ATS-loop, Telegram, мёртвый CL-review |
| `hunter/tracker.py` | 2180 | вне области этого анализа |
| `hunter/filters.py` | 1964 | вне области (листинговые фильтры) |
| `hunter/apply_api.py` | 1204 | пайплайн №1 |
| `hunter/apply_cli.py` | 953 | пайплайн №2 |
| `hunter/dual_apply.py` | 593 | пайплайн №3 (shadow) |
| `generate_docs.py` | 506 | рендер DOCX/PDF |
| `hunter/claim_judge.py` | 576 | судья |
| `hunter/verdict_refine.py` | 481 | refine-петля |
| `prompts/generation_rules.md` | 274 | промпт (общий для обоих пайплайнов) |
| `.claude/commands/apply.md` | 178 | промпт CLI-скилла |

Косвенные метрики связности: **13** комментариев вида `mirror of apply_api Step X`
/ `parity with the API pipeline` в `hunter/*.py`; **236** вызовов `print()` вместо
логгера; **42** ленивых импорта внутри функций в `apply_api.py` (24 в `apply_cli.py`)
— признак того, что модуль тянет полпроекта и импорты разводились от циклов.

---

## 3. Матрица стадий: где копии разошлись

Померено по вхождениям символов в трёх файлах.

| Стадия | API | CLI | Shadow | Комментарий |
|---|:--:|:--:|:--:|---|
| dedup по URL, expired-чек, floor длины | ✅ | ✅ | — | |
| doomed gate / repost gate / prescreen | ✅ | ✅ | — | |
| до-LLM `is_react_only` / `is_backend_only` | ✅ | ❌ | — | CLI ловит стек только ПОСЛЕ генерации |
| `build_ats_keyword_checklist` в первый промпт | ✅ | ❌ | ✅ | CLI-скилл его не получает |
| `build_pl_skip_instruction` | ✅ | ❌ | ✅ | у CLI своя (сломанная в прошлом) логика в `apply.md` |
| `_ats_check_loop` (до 5 раундов rewrite) | ✅ | ❌ | ✅ | CLI полагается на self-score скилла |
| `sanitize_content` | ✅ | ⚠️ | ✅ | CLI получает его косвенно, из `generate_docs.py:412` |
| `_strip_compliance_claims` (DORA/RODO/ISO) | ✅ | ❌ | ✅ | задокументировано как «API only» — но прод на ~23% CLI |
| `_strip_prestige_claims` / `_dedup_skill_glosses` | ✅ | ✅ | ✅ | |
| `run_judge_stage` / `enforce_language_separation` | ✅ | ✅ | ✅ | |
| `ensure_pl_resume` | ✅ | ✅ | ❌ | |
| `content_qa.run_qa` | ✅ | ❌ | ❌ | QA не работает на CLI-ветке вообще |
| `is_bogus_company` | ✅ | ❌ | ❌ | |
| PDF roundtrip / verdict / refine | ✅ | ✅ | ✅ | |
| `run_outreach` | ✅ | ✅ | ❌ | |
| `price_usage` / `set_cost` | ✅ | ❌ | ❌ | осознанно: у подписки нет по-токенной видимости |
| `abort_after_generation` | ❌ | ✅ (6×) | ❌ | нужен только там, где документы уже отрендерены |

**Вывод.** Разница между API и CLI — не «одна стадия генерации», как
предполагал план 05, а **одна стадия генерации плюс пять пропущенных
качественных стадий**. Каждая из них когда-то добавлялась «в оба пайплайна»,
и пять раз про CLI забыли. Это ровно тот класс дефектов, который дал четыре
продовых инцидента за пять недель (PL CV, Interia React, Comarch dedup,
CLI-self-score).

---

## 4. Мёртвый код: подсистема self-review cover letter

Удалена из пайплайна 2026-06-03 коммитом `8bd86ee`
(*«remove cover letter review step — CL accepted as-is after generation»*, PR #70).
Осталась целиком.

| Что | строки в `apply_shared.py` | ~объём |
|---|---|---:|
| Константы: `_REVIEW_SYSTEM`, `_BANNED_OPENER_PATTERNS`, `_BANNED_BODY_PHRASES`, `_BANNED_CTA_PHRASES`, `_CL_WORD_MIN/MAX`, `_CL_BODY_PARA_MIN/MAX`, `_PL_IN_EN_RE`, `_EN_IN_PL_RE`, `_METRIC_RE` | 420–514 | ~95 |
| Функции: `_opener_banlist_hits`, `_body_banlist_hits`, `_cta_banlist_hits`, `_last_paragraph_text`, `_count_body_paragraphs`, `_count_words`, `_count_metrics`, `_detect_language_mixing`, `_review_cover_letter`, `_translate_cover_letter_pl` | 764–1028 | ~265 |
| `_cover_letter_review` + `_cover_letter_review_loop` (deprecated-обёртка) | 2031–2064 | ~34 |
| **Итого** | | **~394 (18% файла)** |

Проверено: ни один из этих символов не вызывается из `apply_api.py`,
`apply_cli.py`, `dual_apply.py`, `verdict_refine.py`. Единственный живой
потребитель — dev-утилита `tools/regen_covers_v2_last3.py`.

**Три следствия, каждое стоит внимания отдельно:**

- CLAUDE.md → «Pipeline Flow → 5. Cover letter self-review loop (up to 3 LLM
  rounds)» описывает то, чего нет. Агент, читающий CLAUDE.md как источник
  правды (а так велено), будет строить решения на несуществующей стадии.
- `tests/test_cover_letter_banlist.py` (129 строк, 18 тестов) + ~39 строк в
  `tests/test_apply_shared.py` тестируют мёртвый код **через `apply_agent`**,
  то есть выглядят как интеграционные. Это отрицательная ценность: они
  зелёные независимо от того, работает ли что-нибудь.
- Качество cover letter сейчас держится **только** на промпте
  (`generation_rules.md`, раздел Cover Letter, две страницы правил) без
  какого-либо пост-контроля. Это может быть осознанным решением 2026-06-03 —
  но оно нигде не записано, а бан-листы лежат рядом и выглядят рабочими.

---

## 5. Настраиваемость: что есть и чего нет

### 5.1 Что уже настраивается (и работает)

| Слой | Механизм | Гранулярность |
|---|---|---|
| Модель генератора | `hunter/llm_profiles.py` + `/llm <name>`, ключ в DB | глобально, рантайм |
| Модель судьи / переводчика | `JUDGE_*` / `TRANSLATE_*` env | глобально, рестарт |
| Треки (angular/react) | `/tracks`, DB-ключ `tracks_enabled` > `CANDIDATE_TRACKS` | глобально, рантайм |
| Личность кандидата | `candidate/candidate.yaml` (`hunter/candidate.py`) | **per-user** (через `CANDIDATE_YAML_PATH`) |
| Фильтры вакансий | `filters.yaml` + `hunter/filter_profile.py` (2 слоя, кэш по mtime) | **per-user**, без рестарта |
| Пороги гейтов | ~20 env-переменных (`ATS_VERDICT_*`, `JUDGE_*`, `PRESCREEN_*`, `DOOMED_GATE_*`, `REPOST_*`, `GEN_SKIP_PL_FOR_EN`, `CV_GDPR_CLAUSE`) | глобально, рестарт |

`filter_profile.py` — **готовый образец** того, как надо делать: Layer 1
(`builtin_defaults()`, общий, ревьюится) + Layer 2 (пользовательский YAML,
мержится по таблице стратегий replace/extend), кэш по `(path, mtime_ns)`,
битово-идентичное поведение при отсутствии файла, невалидные regex
выбрасываются с warning, а не роняют процесс. Путь резолвится **рядом с
`candidate.yaml`** — то есть per-user получается бесплатно, без нового
plumbing (`hunter/filter_profile.py:401-409`).

### 5.2 Блокер №1 — `prompts/generation_rules.md`

Файл в git. 41 из 274 строк — личные факты владельца:

- Таблица «7 разрешённых компаний» с точными периодами (`Alten Poland`,
  `Fairmarkit`, `Venture Labs`, `SII`, `Altoros`, `SolbegSoft`, `Staronka`).
- Правила уровня «у Venture Labs/SII был Java-бэкенд, у SolbegSoft — .NET,
  Node.js только в e-commerce Altoros».
- «jQuery можно добавлять только в до-2022 роли, НИКОГДА в Fairmarkit».
- «10+ years (since 2015)», «Angular (2-XX)» как обязательный формат.
- JSON-схема в конце содержит реальный ВУЗ («Belarusian State Technological
  University»), реальный список курсов и реальный языковой набор
  («Russian (Native), Polish (B1)») — формально «как пример».

Почему это блокер: **оба** пайплайна читают этот файл как единственный
источник правил (`hunter/apply_api.py:430`, `.claude/commands/apply.md:53`).
Второй пользователь не может его поменять, не редактируя tracked-файл
репозитория; а владелец не может поменять свою историю работы, не редактируя
промпт руками — при том что те же самые факты **уже лежат** в `candidate.yaml`
(`employers.real_companies`, `employers.protected`, `employers.flexible`,
`employers.profile_titles`, `education.expected_role_count`).

Прецедент внутри репозитория уже есть: `hunter/verdict_refine.py:60-67`
рендерит блоки промпта (`_PROTECT_LISTED`, `_PLACEMENT_WITH_FLEXIBLE`) из
`candidate.get(...)` в рантайме. То есть «собрать таблицу работодателей в
промпт из YAML» — не изобретение, а распространение уже принятого паттерна.

Побочно: `prompts/judge_rules.md` называет реальных клиентов (Intel,
Atruvia AG, «300+ German banks») — тот же класс проблемы, меньший объём.

### 5.3 Блокер №2 — два источника промпта расходятся

| Что | API-ветка | CLI-ветка |
|---|---|---|
| Выбор base CV по стеку | `apply_api._detect_stack_hint()` + `_BASE_CV_FILES`, **учитывает `candidate.yaml → tracks.base_cv`** | `.claude/commands/apply.md:66-70`, прозой, 5 захардкоженных имён файлов, `tracks.base_cv` **игнорируется** |
| Пропуск PL-полей | `build_pl_skip_instruction()`, гейт по `posting_lang == "EN"` | правило внутри `apply.md` (в июле оно было безусловным — 15 польских работодателей получили английское CV) |
| ATS keyword checklist | инжектится детерминированно | отсутствует |

Пользователь, настроивший `tracks.base_cv` в своём `candidate.yaml`, получит
его на API-ветке и молча не получит на CLI. Это не гипотеза — это тот же
механизм, который дал инцидент с PL CV.

### 5.4 Блокер №3 — политика генерации не per-user

`hunter/users.py::user_env()` (строки 136-156) инжектит три переменных.
Значит для второго пользователя нельзя задать:

`JUDGE_MODE`, `JUDGE_ENABLED`, `ATS_VERDICT_TARGET`, `ATS_VERDICT_MAX_REFINES`,
`PRESCREEN_MODE`, `PRESCREEN_MIN_CONFIDENCE`, `DOOMED_GATE_HARD_ACTION`,
`REPOST_WINDOW_DAYS`, `CV_GDPR_CLAUSE`, `GEN_SKIP_PL_FOR_EN`,
`CANDIDATE_TRACKS`, активный LLM-профиль.

При этом ровно эти ручки владелец реально крутил за последние три месяца
(`ATS_VERDICT_MAX_REFINES` 3→5, `PRESCREEN_MODE` report→warn,
`DOOMED_GATE_HARD_ACTION`, пороги repost-гейта). То есть это не
настраиваемость «на всякий случай», а список того, что уже меняли.

### 5.5 Блокер №4 — константы без ручек

| Константа | Значение | Где | Что решает |
|---|---:|---|---|
| `_ATS_THRESHOLD` | 95.0 | `apply_shared.py:1272` | когда ATS-loop доволен |
| `_ATS_MAX_ROUNDS` / `_TOTAL_ROUNDS` | 2 / 5 | `apply_shared.py:1273`, `:1886` | сколько честных vs агрессивных раундов |
| `_ATS_CHECKLIST_CAP` | 30 | `apply_shared.py:1308` | размер keyword-чеклиста в промпте |
| `_REACT_SKIP_MIN_MENTIONS` | 3 | `apply_shared.py:153` | чувствительность react-скипа |
| `SIM_HARD` / `SIM_COMPANY` / `SIM_WARN` / `MIN_TEXT_CHARS` | .94 / .90 / .85 / 1500 | `repost_gate.py:56-59` | откалибровано, но «зашито» |
| `STRETCH_FROM_ROUND` | 4 | `verdict_refine.py:53` | с какого раунда разрешено «растягивать» |
| `HEAL_DELTA_PP` | 5.0 | `ats_pdf_roundtrip.py:46` | порог NBSP-самолечения |
| `CANONICAL_ANGULAR_SKILL` | `"Angular (2-22)"` | `content_qa.py:233` | личная деталь в общем коде |

Часть из них **правильно** захардкожена: откалиброванные пороги repost-гейта
менять вслепую вреднее, чем не менять вообще. Но `_ATS_THRESHOLD` и число
раундов — прямые деньги и прямое качество, и у них нет ни env, ни YAML.

### 5.6 Блокер №5 — вёрстка документа зашита в код

`generate_docs.py` жёстко задаёт: шрифт `Calibri`, кегли 16/13/11/10, поля
0.8/0.5/1.0/1.0 см, порядок и названия секций (`SUMMARY`, `SKILLS`,
`EXPERIENCE`, `EDUCATION`, `ADDITIONAL COURSES`), четыре категории навыков
(`Frontend / Tools / Methodologies / Languages`), шаблон заголовка
`{headline} ({stack})`. Другой кандидат не может ни переименовать секцию, ни
добавить пятую категорию навыков, ни убрать `ADDITIONAL COURSES`.

Побочная находка: `generation_rules.md:117` велит LLM использовать секцию
**`WORK EXPERIENCE`**, а `generate_docs.py:249` осознанно рендерит
**`EXPERIENCE`** (комментарий: Taleo классифицирует «WORK EXPERIENCE» как
«Other»). Практического вреда нет — заголовки рисует рендерер, — но это
расхождение внутри одного контракта.

### 5.7 Нерешённое противоречие двух стадий

Зафиксировано в `AGENT_LOG` 2026-08-22 и намеренно не тронуто:
`_ats_check_loop` вписывает ключевые слова вакансии в Skills → claim judge
помечает их как `fabrication` (1658 находок на 323 отчётах; топ: GraphQL 21×,
Karma 11×, Vite 8×, Figma 8×) → repair их вырезает. Две стадии отменяют работу
друг друга, и обе платят за LLM.

С точки зрения настраиваемости это не баг, а **отсутствующая ручка**:
«насколько агрессивно вписывать чужие ключевые слова» и «насколько строго
судья карает за вписанное» — это одно решение, выраженное в двух независимых
подсистемах. Пока оно не выражено одним параметром, настройка одной стороны
ломает другую.

---

## 6. Предложение: что декомпозировать и в каком порядке

Порядок выбран так, чтобы каждая волна была самостоятельно полезна, а самая
рискованная (унификация пайплайнов) шла последней и на уже уменьшенном объёме.

### Волна 0 — «бесплатное» (1 PR, только удаление и документация)

- Удалить мёртвую CL-подсистему (~394 строки) вместе с
  `tests/test_cover_letter_banlist.py` и CL-частью `test_apply_shared.py`.
  Если бан-листы ценны — тогда наоборот, **включить** `_cover_letter_review`
  обратно в оба пайплайна. Но «лежит, документировано, тестировано и не
  вызывается» — худший из трёх вариантов.
- Починить CLAUDE.md → Pipeline Flow (убрать Step 5) и
  `docs/PUBLIC_RELEASE_CHECKLIST.md` §1 (галочка не соответствует реальности).
- Расширить `tests/test_handoff_readiness.py`: сканировать не только `*.py`,
  но и `prompts/*.md` + `.claude/commands/*.md`. Сегодняшний прогон **должен
  упасть** — это и есть подтверждение находки §5.2.

**Критерий готовности:** `grep -c "_cover_letter_review" hunter/ *.py` → 0;
readiness-тест красный до волны 2 и зелёный после.

### Волна 1 — разбор `apply_shared.py` (чистые переносы, поведение не меняется)

2211 строк / 53 функции → пакет `hunter/pipeline/` (имя согласовано с планом 05,
чтобы волна 4 въехала в готовую структуру):

| Новый модуль | Что переезжает | ~строк |
|---|---|---:|
| `pipeline/errors.py` | `ApplyError`, коды выхода, `is_rate_limit_error`, `is_transient_fetch_error` | ~95 |
| `pipeline/notify.py` | `notify`, `send_telegram_documents` | ~183 |
| `pipeline/gates.py` | `run_doomed_gate`, `run_prescreen`, `stack_gate_allows_manual`, `is_react_only_job_text`, `is_backend_only_job_text`, `_already_processed` | ~270 |
| `pipeline/scrubs.py` | compliance / prestige / gloss (10 функций) | ~370 |
| `pipeline/lang.py` | `enforce_language_separation`, `_translate_*`, `ensure_pl_resume`, `build_pl_skip_instruction`, `_is_unit_clean` | ~330 |
| `pipeline/ats.py` | `_ats_check_loop`, `build_ats_keyword_checklist`, `_filter_self_description_keywords` | ~230 |
| `pipeline/abort.py` | `abort_after_generation`, `_write_abort_skip_row`, `_handle_jobleads_fetch_blocked` | ~200 |
| `pipeline/folders.py` | `compute_output_folder`, `_sanitize_folder_company` | ~28 |

`apply_shared.py` остаётся ре-экспорт-шимом (тот же приём, что с
`telegram_bot.py` в Phase 1-7) — ни один существующий импорт не ломается,
диффы ревьюятся как переносы. Это делает пункт «PR-финал-3» плана 05 ненужным
и **снимает риск** с волны 4.

### Волна 2 — деперсонализация промптов

1. Разделить `prompts/generation_rules.md` на два:
   - tracked, общий, **без единого личного факта** — структура, RED LINES про
     язык / выдумки / прославление, JSON-схема с обезличенным примером;
   - блок, **рендерящийся в рантайме** из `candidate.yaml`: таблица
     работодателей с периодами, правила «какой стек у какой роли», годы опыта,
     формат версии основного фреймворка. Ровно тем же способом, что уже
     работает в `verdict_refine.py:60-67`.
2. Опциональный per-user хвост `{cand_dir}/generation_rules.local.md`,
   приклеиваемый после общей части — для правил, которые не выражаются
   структурой (личный тон cover letter, персональные табу).
3. То же для `prompts/judge_rules.md` (реальные клиенты → из `candidate.yaml`).
4. Свести выбор base CV к одному источнику: `.claude/commands/apply.md` должен
   получать карту стеков **из `candidate.yaml`**, а не хранить свою.

**Критерий готовности:** readiness-тест из волны 0 зелёный; smoke-прогон с
вымышленным `candidate.yaml` даёт CV без единого упоминания реальных
работодателей владельца.

### Волна 3 — `generation.yaml` (профиль генерации)

Точная копия архитектуры `filter_profile.py`, без изобретений:

```
hunter/gen_profile.py
    builtin_defaults()        # Layer 1: сегодняшние значения, ревьюится
    load_gen_profile(path)    # Layer 2: YAML рядом с candidate.yaml
    # кэш по (path, mtime_ns); файла нет → Layer 1 бит-в-бит
```

Путь резолвится как у фильтров — рядом с `candidate.yaml`, поэтому per-user
получается автоматически, **без правки `user_env()`** для файловых настроек.
Черновик набора ручек (каждая — с дефолтом = сегодняшнее значение):

```yaml
ats:
  threshold: 95             # _ATS_THRESHOLD
  honest_rounds: 2          # _ATS_MAX_ROUNDS
  total_rounds: 5           # _TOTAL_ROUNDS
  checklist_cap: 30         # _ATS_CHECKLIST_CAP
  keyword_injection: honest # honest | aggressive   ← см. §5.7
verdict:
  target: 95
  max_refines: 5
  stretch_from_round: 4
judge:
  enabled: true
  mode: warn                          # report | warn | block
  repair_severities: [fabrication]    # ← вторая половина ручки §5.7
gates:
  prescreen_mode: warn
  doomed_hard_action: skip
  repost_window_days: 60
document:
  font: Calibri
  sections: [SUMMARY, SKILLS, EXPERIENCE, EDUCATION, ADDITIONAL COURSES]
  skill_categories: [frontend, tools, methodologies, languages]
  gdpr_clause: both
  margins_cm: {top: 0.8, bottom: 0.5, left: 1.0, right: 1.0}
```

Пороги repost-гейта в YAML **не выносить** — они откалиброваны на реальном
корпусе (`tools/reuse_calibrate.py`), и ручка приглашает сломать их вслепую.

Граница правила, симметричная правилу дока 08 («сменился человек →
`candidate.yaml`; сменился режим → `.env`»): **«что система пишет в CV →
`generation.yaml`; как система устроена (таймауты, очереди, интеграции) →
`.env`»**. Приоритет: env > YAML > builtin — тот же порядок, что у фильтров
(env остаётся аварийным рычагом).

### Волна 4 — унификация пайплайнов (план 05)

Главное, что стоит зафиксировать: **предусловие плана 05 выполнено.** Он
требовал «не начинать без док 04 (golden E2E)»; с 2026-08-24 golden-тесты есть
у **обоих** пайплайнов (`tests/test_golden_apply_e2e.py` +
`tests/test_golden_apply_cli_e2e.py`). Страховка на месте.

Что стоит поправить в самом плане перед стартом:

- Инвентаризация в нём устарела (см. §2) — цифры выросли.
- Первым шагом PR-0 он требует «инвентаризацию различий зеркал». Она сделана
  в §3 этого документа и содержит **пять содержательных пропусков**, а не
  только «CLI отстаёт намеренно». Каждый из пяти — отдельное решение
  (включить на CLI / признать неприменимым), и принимать его надо **до**
  переноса, иначе рефакторинг зафиксирует пропуск в коде стадий.
- Волна 1 этого документа делает его PR-финал-3 ненужным.

---

## 7. Что нельзя решить без живых данных

Локальный воркtree данных не содержит. Для этих вопросов нужен деплой-хост
(`docker compose exec job-hunter python ...`):

1. **Реальная доля `main_cli`** сегодня. От неё зависит срочность пяти
   пропусков из §3. Последняя известная оценка — 14/60 ≈ 23%
   (`AGENT_LOG` 2026-08-24).
2. **Цена конфликта §5.7**: сколько LLM-раундов и сколько $ уходит на
   «вписали → судья пометил → вырезали». Инструменты есть —
   `tools/judge_stats.py` + `cost_usd` из `tracker.db`.
3. **Стоит ли `_ATS_THRESHOLD=95` своих денег**: коррелирует ли высокий
   ATS-score с `sent / confirmed / answered`. Инструмент есть —
   `tools/verdict_funnel_corr.py`.
4. **Какие ручки реально разошлись бы между пользователями** — сейчас
   пользователь один, и весь §5.4 опирается на «владелец крутил это сам»,
   а не на измеренную потребность второго пользователя.

---

## 8. Явные не-цели

- **Никакого DSL / YAML-описания самого пайплайна.** Уже отвергнуто в плане 05,
  и §5 не даёт ни одного аргумента пересматривать: настраивать надо
  *содержание* документа, а не *порядок стадий*.
- **Никакого нового LLM-слоя.** Постоянное правило владельца: каждый LLM-вызов
  обязан менять реальное решение. §5.7 — скорее аргумент убрать один вызов,
  чем добавить.
- **Никаких ручек «на будущее».** Каждая строка в `generation.yaml` из волны 3
  соответствует значению, которое владелец уже менял руками, либо которое
  вырезано из промпта в волне 2.
- **Не трогать `tracker.py` / `filters.py`** — они большие, но это отдельная
  область; смешивать её с генерацией нельзя.
