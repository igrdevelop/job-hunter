# Verdict Refine Loop — дожать независимый вердикт честными правками

**Branch:** `feat/verdict-refine-loop` (from `origin/master` @ 1f969ed)
**Status:** PLANNED — ready for implementation
**Scope:** new `hunter/verdict_refine.py`, wiring in `apply_api.py` + `apply_cli.py`, config, tests, CLAUDE.md

---

## Зачем (по-русски, для владельца)

Сейчас независимый вердикт (Haiku оценивает отрендеренный EN PDF против вакансии)
считается **один раз, в самом конце** — и его фидбек просто записывается в таблицу.
При этом вердикт возвращает конкретный список: каких ключевых слов не хватает и что
рекомендуется добавить. Половина этих пунктов — вещи, которые у кандидата **есть**,
но резюме их не показало (REST/HTTP, Docker, accessibility, AWS-примеры).

**Было:**

```
генерация CV → рендер PDF → вердикт (92) → записали в таблицу. Конец.
```

**Станет:**

```
генерация CV → рендер PDF → вердикт (92)
  → вердикт < 95? → переписать резюме ПО ФИДБЕКУ вердикта
       (только факты из candidate_profile.md — судья-джадж перепроверит)
  → пере-рендер PDF → новый вердикт (96?) 
  → лучше стало — оставляем; хуже/так же — откатываем к прошлой версии
  → максимум ATS_VERDICT_MAX_REFINES раундов (по умолчанию 1)
```

**Честные ожидания.** Это добавит +3–8 пунктов там, где гэп «презентационный»
(навык есть, но не показан). Там, где гэп реальный (вакансия про Vue, локация США),
вердикт НЕ вырастет — и это правильно: рекомендации типа «переезжайте в Вирджинию»
цикл игнорирует, а выдумки вычистит claim judge. Гарантии «всегда 95+» нет и быть
не может без вранья.

**Цена.** Один раунд ≈ $0.05–0.07 (rewrite Sonnet ~$0.04 + judge-перепроверка ~$0.01
+ новый вердикт Haiku ~$0.01; рендер PDF локальный, бесплатный). Раунд запускается
только когда вердикт ниже цели, т.е. типичная вакансия подорожает с ~$0.19 до ~$0.25.

**Плюс одно мелкое изменение по просьбе владельца:** self-score («сам себя оценил
на 96%») убирается из интерфейсов — в Telegram остаётся только вердикт, в трекере
колонка «ATS %» после стампа получает значение вердикта.

---

## Ключевые решения (зафиксированы, не обсуждаются в PR)

1. **Цель** `ATS_VERDICT_TARGET = 95`, **раунды** `ATS_VERDICT_MAX_REFINES = 1`
   (0 = выключено, можно поставить 2). Оба — env-переменные в `hunter/config.py`.
2. **Keep-best guard:** новый вердикт СТРОГО больше старого → принимаем новую
   версию; иначе откатываем content.json и пере-рендерим старую версию. Регресс
   невозможен по построению.
3. **Только честные правки:** rewrite-промпт получает candidate_profile.md и жёсткое
   правило «добавлять только то, что подтверждено профилем; ничего не выдумывать».
   После rewrite контент повторно проходит скрабы + claim judge + языковой гейт —
   те же ворота, что и первая генерация. Judge — enforcement, промпт — intent.
4. **Неисправимые рекомендации отфильтровываются детерминированно** до промпта:
   пункты про location/relocate/hybrid/on-site, «add a cover note», «update LinkedIn»
   выкидываются regex'ом — они не про текст CV.
5. **Правится только `resume_en`** (вердикт измеряет EN PDF). Исключение: если
   `primary_lang == "PL"` (польская вакансия, PL CV тоже отправляется) — принятые
   правки зеркалятся в `resume_pl` существующим translate-хелпером, чтобы CV не
   разъехались. Cover letters цикл не трогает вообще.

---

## M1 — `hunter/verdict_refine.py` (новый модуль, вся логика в одном месте)

Two functions, pure orchestration (no Telegram, no tracker inside):

```python
def build_refine_feedback(verdict: dict) -> str | None:
    """missing_keywords + recommendations + gap_report → feedback text for the
    rewrite prompt. Deterministically DROPS non-CV items (location/relocation/
    hybrid/on-site/cover note/LinkedIn — regex list). Returns None when nothing
    actionable survives (then the loop is a no-op)."""

def refine_loop(content, job_text, base_cv, folder, verdict, *,
                regenerate_docs, target, max_rounds) -> tuple[dict, dict]:
    """The loop. Per round:
      1. feedback = build_refine_feedback(verdict); None → stop.
      2. call_llm (active profile, same system prompt as generation:
         candidate_profile + generation_rules) with the current resume_en,
         the feedback list and the honesty constraint → revised resume_en.
      3. Re-run the safety stages on the revised content: sanitize → 
         _strip_compliance_claims/_strip_prestige_claims/_dedup_skill_glosses →
         run_judge_stage (mode from config, capped to "warn" here — the refine
         loop never blocks; survivors just logged) → enforce_language_separation
         (block signal → discard this round, keep previous version).
      4. Mirror to resume_pl via the translate helper IF primary_lang == "PL".
      5. Write content.json, call regenerate_docs(folder) (injected callable —
         apply_api/apply_cli pass their own generate_docs invocation).
      6. New verdict = ats_pdf_roundtrip.run_llm_verdict(folder, job_text).
      7. new.score > old.score → accept (content/verdict become current);
         else → restore previous content.json + regenerate_docs once (rollback).
    Returns (final_content, final_verdict)."""
```

Best-effort: любое исключение внутри раунда → лог + возврат текущей лучшей версии.

## M2 — wiring в `apply_api.py` (Step 7.7)

Точка: сразу после первого `run_llm_verdict` (сейчас `apply_api.py:760`), до
стампа в tracker. Если `verdict["score"] < ATS_VERDICT_TARGET` и
`ATS_VERDICT_MAX_REFINES > 0` — вызвать `refine_loop(...)`; дальше по коду идёт
УЖЕ финальный вердикт (стамп, cost re-price, Telegram) — существующие строки не
дублировать, просто подставить результат цикла. Cost-учёт: расходы rewrite- и
verdict-вызовов раундов включаются в `content["cost"]` тем же механизмом, что
существующий verdict re-price.

## M3 — wiring в `apply_cli.py`

Та же вставка в post-verdict блоке (`apply_cli.py:548`), через тот же
`refine_loop` с CLI-вариантом `regenerate_docs`. Если у CLI-режима нет API-ключа
генератора (rewrite идёт через `call_llm`) — цикл молча скипается с логом.

## M4 — «только вердикт» в интерфейсах

- `tracker.set_ats_verdict(url, score)` дополнительно пишет score в `ats_status`
  (колонка «ATS %») — Sheet колонка E после ресинка показывает вердикт, а не
  self-score. Колонка N и `verdict_writer` не трогаются (история + backfill).
- Telegram-уведомление: убрать `| self: NN%` — остаётся только
  `ATS: NN% (independent, PDF)`. Генераторный self-score остаётся в content.json
  (для диагностики), но в интерфейсах не показывается.
- `/funnel` не ломается: «generated» = numeric `ats_status`, вердикт тоже numeric.

## M5 — config + docs

- `hunter/config.py`: `ATS_VERDICT_TARGET` (default 95), `ATS_VERDICT_MAX_REFINES`
  (default 1). Добавить в `.env.example` с комментарием.
- CLAUDE.md: обновить Step 7a описание пайплайна + config-таблицу + Work Log.
- Этот файл: Status → DONE.

## M6 — tests (`tests/test_verdict_refine.py` + правки существующих)

1. `build_refine_feedback`: location/cover-note/LinkedIn рекомендации выкинуты;
   пустой остаток → None.
2. `refine_loop`: score вырос → принято, content.json перезаписан, verdict новый.
3. score НЕ вырос → rollback: старый content.json восстановлен, regenerate_docs
   вызван повторно, возвращён старый verdict.
4. Языковой гейт сигналит block на revised-версии → раунд отброшен, старая
   версия жива.
5. Исключение в rewrite-вызове → best-effort, возвращена исходная пара.
6. `max_rounds=0` / verdict ≥ target → цикл не запускается (0 LLM-вызовов).
7. `primary_lang=="PL"` → translate-хелпер вызван для resume_pl; `"EN"` → нет.
8. `set_ats_verdict` теперь обновляет и `ats_status` (+ существующие тесты
   стампа поправить).
9. Telegram-формат: строка без `self:` (поправить существующий тест формата).

## Explicitly OUT of scope

- Dual-apply shadow: цикл в shadow НЕ добавляем в этом PR (сначала мержится
  `feat/dual-shadow-parity`; потом refine добавится туда одной строкой через тот
  же `refine_loop` — иначе конфликт гарантирован).
- Блокировка доставки при вердикте ниже цели — НЕ делаем (вердикт остаётся
  информационным; владелец решает сам по числу в Telegram).
- Фильтрация вакансий на входе по гэпам (пункт 2 из обсуждения) — отдельная
  ветка, не сюда.
- Никаких изменений в `_ats_check_loop` (keyword-цикл) — он остаётся как есть.

## Acceptance criteria

- [ ] `pytest tests/` green (≥9 новых тестов), `ruff check .` clean,
      `python -m compileall .` OK.
- [ ] Прогон на реальной вакансии (tools/preview_apply.py или боевой /force):
      в логе видно `verdict 92 → refine round 1 → verdict NN`, в content.json
      финальный вердикт, файлы пере-рендерены.
- [ ] При `ATS_VERDICT_MAX_REFINES=0` пайплайн байт-в-байт ведёт себя как сейчас
      (кроме M4-косметики).
- [ ] Регресс вердикта невозможен: в тестах явная проверка rollback-ветки.
- [ ] В Telegram-карточке только независимый вердикт, self-score отсутствует.
