# Data & Evaluation Plan — измерять, прежде чем менять

**Status:** draft
**Date:** 2026-09-09
**Роль:** приглашённый data scientist / ML-evaluation (аудит агентом,
read-only; живые данные живут на VPS, локальный tracker.db это 14-строчная
фикстура, поэтому все замеры оформлены как скрипты для деплой-хоста)
**Motivation:** система принимает дорогие решения (5 раундов refine, выбор
модели, набор источников) вокруг чисел, чья связь с реальным исходом не
измерена. Два вопроса из июльского ревью открыты; данных для полноценной
статистики мало, но достаточно, чтобы отсечь крупные ошибки. Плюс продукт не
пишет ни одного события, по которому можно будет ответить на следующий вопрос.

---

## Что уже измеряется

| Вопрос | Инструмент/колонка | Файл | Достаточно ли данных |
|---|---|---|---|
| Воронка tracked→generated→sent→confirmed→answered, общая и по источникам | `compute_funnel(days)`; источник выводится из URL, в БД не хранится | `hunter/funnel.py`, `/funnel` | Да для tracked/generated/sent (~700 строк); answered единицы-десятки, per-source почти всегда n<10 |
| Предсказывает ли вердикт исход | бэнды `<80…95+` × sent/confirm/answer-rate; «вывод» = spread ≥10pp (не тест) | `tools/verdict_funnel_corr.py` | Не запускался на проде; правило spread≥10 некорректно при n≈20 на бэнд |
| Шум Haiku-судьи на неизменном PDF | per-folder spread, population σ | `tools/verdict_noise.py` | Нужен прогон на VPS (~30 Haiku-вызовов, <$1). Без σ вывод про target бессмыслен |
| Классы нарушений judge | агрегация `judge_report.json` | `tools/judge_stats.py` | Смещено: файл пишется только при violations (`apply_api.py:850`), знаменатель не сохраняется; факт repair не персистится (`claim_judge.py:400`) |
| Похожесть вакансий / репосты | offline TF-IDF replay, пороги 0.85/0.90/0.94 | `tools/reuse_calibrate.py`, `hunter/repost_gate.py` | Да, $0; готовая основа для market-aggregate |
| Калибровка гейтов против Sent | `assess_job_text` ($0) / `assess_stack` (Haiku) vs `sent_parse.classify` | `tools/screen_calibrate.py`, `tools/prescreen_calibrate.py` | Да; ground truth «владелец отправил», не «ответили» |
| Живость скрейперов | `source_runs`, ring-buffer 50 записей/источник | `hunter/source_health.py` | Только «сломан/жив»; тренда yield за месяцы нет |
| Стоимость/вакансия | `cost_usd`, per-call токены в `content["cost"]` | `hunter/db.py`, `llm_client._record_usage` | Да для API; NULL для CLI (в проде с августа) |
| Финальный вердикт + история refine | `ats_verdict` (БД); `content["verdict_history"]` | `hunter/tracker.py:1806`, `hunter/verdict_refine.py:300-556` | История только в content.json |
| $0 pre-score vs LLM-вердикт | `content["ats_check"]`, `content["ats_check_pdf"]`, `content["ats_verdict"]` | `hunter/pipeline/ats.py:235`, `hunter/ats_checker.py:310` | Пара в каждом content.json, не в БД; backfill даст ~500 пар за $0 |
| Подтверждение ATS / ответ человека | `confirmation` (авто из Gmail); `answer` только ручная правка в Sheets | `hunter/email_response_checker.py:559-579` | confirmation смещён по источникам (Easy Apply ≠ Greenhouse); answer свободный текст |
| A/B моделей | пары content.json primary vs `{shadow}/` с вердиктом одного судьи | `hunter/dual_apply.py:412` | ≈86 shadow-папок; статистики нет; 0/12 августовских без вердикта |
| Причины провалов | `logs/apply_failures.jsonl`, транскрипты 7 дней | `hunter/apply_failures_log.py` | duration только для FAIL |

## Слепые зоны

- **Нет временных меток стадий**: в `applications` только `date` (день). Нельзя ответить «сколько идёт генерация», «где тратится время refine».
- **Источник, модель/профиль, пайплайн (api/cli), трек, язык постинга не на строке**: любой разрез требует парсинга папок.
- **История refine в БД отсутствует**: «какой раунд победил», «дельта stretch vs honest» не считаются.
- **Judge без знаменателя и без факта repair**.
- **Действия пользователя неразличимы**: Skip-кнопка (`url_message.py:48`), doomed gate (`gates.py:180`), abort, dedup пишут одинаковый SKIP. Правки CV после генерации не фиксируются.
- **Исходные метки бедные**: `answer` свободный текст; нет enum interview/rejection/offer/silence, нет даты ответа, нет right-censoring.
- **CLI-режим слеп по стоимости и модели**.
- **Корпус постингов только для того, к чему применились** (~10% листингов): для market-aggregate смещённая выборка.
- **source_runs обрезается до 50 записей**.

---

## M0 — Два июльских вопроса (read-only, ≈$1)

### M0.1 Предсказывает ли вердикт ответ?

Новый `tools/verdict_vs_outcome.py` (обещан в `path-A` A3, не написан).
```bash
docker compose exec -T job-hunter python tools/verdict_vs_outcome.py --min-age-days 21
docker compose exec -T job-hunter python tools/verdict_noise.py --n 15 --k 3
```
Анализ:
1. Выборка: `ats_verdict IS NOT NULL`, `sent_parse.classify(sent)=="applied"`,
   `parse_sent_date(sent) ≤ today−21` (цензурирование тишины). Исключить
   `cost_usd IS NULL` за 2026-08-07..08-10 (CLI-вердикты не от Haiku).
2. Не бэнды, а непрерывный вердикт: точечно-бисериальная корреляция +
   логистическая регрессия `answered ~ verdict/10 + source_bucket`
   (linkedin / polish boards / ats-direct: главный конфаундер, Easy Apply
   конвертирует иначе, чем Greenhouse). Без scipy: bootstrap 2000 ресемплов
   для 90% CI odds-ratio на +10pp; Fisher exact на верхнюю vs нижнюю терцили.
3. Второй разрез с n≈500 из `verdict_history`: доля принятых раундов по kind
   (honest/stretch), средняя дельта принятого раунда, доля вакансий, где
   победил раунд ≥4.
4. То же для `confirmed`, отдельной строкой с пометкой «прокси ATS-ack».

Реальность выборки: ~700 строк → ~250–300 sent → при answer-rate 5–10%
**15–30 событий**. Чтобы отличить 8% от 16% с мощностью 0.8, нужно ~200 sent
на плечо. Уверенно ловится только крупный эффект (5% vs 20%); отсутствие
значимости ≠ отсутствие эффекта.

Правило решения (до запуска):
- answered ≥ 15 и 90% CI OR(+10pp) целиком > 1 → target 95 остаётся.
- answered ≥ 15 и CI включает 1 → `ATS_VERDICT_TARGET` 95→88 и
  `ATS_VERDICT_MAX_REFINES` 5→2 как env-эксперимент на 6 недель.
- answered < 15 → корреляцию не трактовать; решать по п.3: если
  stretch-раунды принимаются < 20% случаев или средняя дельта < 2σ из
  `verdict_noise.py` → `MAX_REFINES` 5→3 (убрать stretch), target не трогать.

### M0.2 Какие источники кормят воронку?

Новый `tools/funnel_sources.py` поверх `compute_funnel(90)`: к каждому
источнику Wilson 90% CI для sent/generated, `sum(cost_usd)/sent` (CLI-строки
«unpriced»), число FAIL и SKIP, статус из `health_report()`. Правило:
- tracked ≥ 30 за 90д и sent = 0 → «балласт»: выключить `*_ENABLED`, сначала
  глазами 10 отфильтрованных (скрипт печатает URL).
- `status ∈ {BROKEN?, ERROR}` ≥ 14 дней → чинить или выключить.
- sent ≥ 5 и answered = 0 при ≥ 21 дне → НЕ выключать (n мал), флаг «watch».
Итог: таблица решений в `docs/review-2026-07/`.

Тест обоих: pure-функции над списком dict (как `compute_bands`), фикстуры
in-memory tracker.db по образцу `tests/test_funnel.py`; синтетика с известным OR.

## M1..Mn — Milestones

### M1 — Журнал прогонов и событий (одна БД, $0)

Три таблицы в tracker.db (DDL идемпотентно в `hunter/db.py`, как
`subsystem_health`; в Postgres переезжают как есть, `03-ARCHITECTURE_PLAN.md`):

```
generation_runs(run_id PK, user_id, url_norm, row_id, started_at, finished_at,
  pipeline api|cli, profile, gen_model, judge_model, track, posting_lang, source,
  is_manual, is_force, ats_pre_score, ats_pre_keyword, ats_pdf_score,
  verdict_first, verdict_final, refine_rounds, refine_accepted, best_round_kind,
  judge_violations, judge_repaired, judge_surviving, lang_gate_hits, lang_gate_blocked,
  scrub_fixes, reused_donor, cost_usd, outcome, exit_code)
pipeline_events(id PK, run_id, ts, stage, event, duration_ms, payload JSON)
user_events(id PK, ts, user_id, url_norm, kind apply_click|skip_click|sent_stamp|
  outcome_label|doc_edit|variant_chosen|profile_confirmed|document_downloaded, payload JSON)
```

`generation_runs` это та же таблица, что `usage_events` в `06-OPS_PLAN.md` M5
и event log в `01-PRODUCT_PLAN.md` M1: одна реализация на три плана. Файлы:
`hunter/metrics.py` (`start_run/stage/finish_run`, всё в
`best_effort("metrics")`), точки записи в `apply_api.py` (Steps 1.5, 3, 4a,
5a-bis, 5b, 7, 7a, 7b) и `apply_cli.py`; `url_message.py` → `user_events`;
`claim_judge.py`: `fixes` в `judge_report.json` и всегда писать файл (пустой
`violations: []` даёт знаменатель). Метка исхода: колонка `outcome_label`
(enum interview/rejected/offer/silence) + `outcome_at`, сеттер `/outcome`
в Telegram и валидированная колонка в Sheets. **Backfill**:
`tools/backfill_runs.py` парсит `Applications/**/content.json` → ~500 строк
истории. Тест: golden E2E утверждает одну строку `generation_runs` с
`verdict_first/final`, `judge_violations`; mutation-verify по стадии 7b.

### M2 — Offline eval harness для промптов/моделей

`tools/eval_golden.py`: golden set из 50 папок, стратифицированных по
source×lang×track, зафиксированных в `tests/fixtures/golden_set.json` (путь +
sha256 `job_posting.txt`), на VPS. Кандидат-вариант генерирует content.json в
`eval_runs/<tag>/`; метрики без LLM: `ats_checker.check(run_llm_review=False)`,
`lang_guard.scan_content`, `validate_content`, `content_qa.run_qa`, число
ролей/буллетов, длина. Платные опционально: `claim_judge.judge_content`
(~$1/50) и `run_llm_verdict` (~$0.5). Сравнение baseline vs candidate
**парное**: разность по папке, bootstrap 95% CI, sign-test/Wilcoxon (numpy);
ворота: candidate принимается, если CI средней Δ deterministic score ≥ −1pp,
lang-gate hits не выросли, ошибок валидации 0. Модели по dual-парам:
`tools/dual_pairs_stats.py`: доля shadow ≥ primary с Wilson CI, средняя
разность с bootstrap CI, разности < 2σ помечены «неразличимо», стоимость
обеих сторон. Генерация golden-прогона ~$15 на Sonnet; оценка $0.

### M3 — Market-aggregate: замер и модель данных

**M3.0 (замер, $0)**: `tools/market_m0.py` по `Applications/**/job_posting.txt`
(shadow исключить). Термы: `ats_checker.extract_job_keywords` +
`TfidfVectorizer(ngram_range=(1,3), min_df=3, stop_words EN+PL)`. Ячейка =
role_family (regex по title: angular/react/frontend-generic/fullstack) ×
region (whitelist городов из `filter_profile`, remote-токен). Дедуп:
`text_hash` + коллапс пар с TF-IDF cos ≥ 0.94. Разбить хронологически на две
половины, для ячеек с n ≥ 30 сравнить share термов: Jaccard top-30, Spearman.
Правило: Jaccard ≥ 0.7 и Spearman ≥ 0.6 хотя бы для «angular × poland-remote»
→ агрегат стабилен, строим; иначе укрупнить ячейки/окно. Полезность: среднее
покрытие top-30 термами каждого held-out постинга ≥ 50%. Оговорка: корпус
это прошедшие фильтр вакансии одного кандидата, смещение.

**M3.1 модель данных**: `postings_seen(posting_id, url_norm, source,
first_seen, last_seen, title, company, location, remote_flag, lang, salary_raw,
skills_listing JSON, text_hash, text_path NULL)` пишется из hunt-цикла для
ВСЕХ листингов ($0, JustJoin отдаёт `requiredSkills`, NoFluff `requirements`);
`posting_terms(posting_id, term, tfidf, in_requirements)`;
`demand_profile(role_family, region, period, term, n_postings, doc_freq,
share, ci_low, ci_high, rank)`. Ночной `scheduled_market_aggregate`. Только
источники из allowlist `07-COMPLIANCE_PLAN.md` M6; k ≥ 10 на ячейку. Проверки:
n ≥ 30, Wilson CI, дрейф Jaccard к прошлой неделе ≥ 0.6 (иначе `best_effort`
алерт). Тест: 40 синтетических постингов с известными частотами; репост
считается один раз.

### M4 — Статистика сравнений вместо A/B

**A/B у клиентов при малом объёме: нет.** При ~6 откликах/день и answer-rate
~8% для детекции 8→12% нужно ~1500 отправок на плечо. Вместо этого:
1. offline-ворота M2 как регрессионный барьер перед любым изменением
   промпта/модели;
2. парные внутривакансионные сравнения: dual-apply уже даёт пары; расширить
   до «два варианта одной вакансии, клиент выбирает, какой отправить»
   (`user_events.kind=variant_chosen`), парный preference-сигнал с мощностью
   в разы выше;
3. последовательный мониторинг answer-rate (CUSUM / Beta-posterior по
   неделям) как сторож деградации, не как тест гипотез;
4. бэнды вердикта показывать клиенту только после M0.1.

---

## Risks

| Риск | Чем ловится |
|---|---|
| M0.1 даст «нет сигнала» из-за малого n и это прочитают как «вердикт бесполезен» | Правило решения различает «CI включает 1» и «answered < 15» |
| `generation_runs` замедлит apply | `best_effort("metrics")`, запись после стадии, не внутри |
| `postings_seen` растёт на тысячи строк в неделю | TTL 180 дней, только allowlist-источники |

## Cost

M0 ≈ $1 (verdict_noise). M1–M3 $0. M2 golden-прогон ~$15 на кандидата.

## Open questions

Решено 2026-09-12 (см. README, «Решения владельца»): предварительного правила
про refine НЕ будет. `tools/verdict_vs_outcome.py` по-прежнему печатает
правило решения рядом с числами — это остаётся полезной рамкой для чтения
отчёта, — но `ATS_VERDICT_TARGET`/`ATS_VERDICT_MAX_REFINES` меняются только
отдельным решением владельца после того, как цифры увидены. Пункт 2 ниже
закрыт этим же ответом.

Открыто:

1. Запустить M0.1/M0.2 + `verdict_noise.py --n 15 --k 3` на VPS на этой неделе (да/нет)?
3. `outcome_label` + `/outcome` вводить сейчас, до SaaS (да/нет)?
4. Всегда писать `judge_report.json` + `fixes` (рост числа файлов на Drive) (да/нет)?
5. Персистить все листинги hunt-цикла в `postings_seen` (да/нет)?
6. Golden set: sha256/пути 50 папок в репо, тексты на VPS (да/нет)?
7. Отказ от клиентского A/B в пользу «клиент выбирает версию» как продуктовое решение (да/нет)?
