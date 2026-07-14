# 05 — Унификация apply-пайплайнов: один стадийный раннер

**Приоритет:** P1→P2 · **Усилие:** ~неделя, строго поэтапно · **Ветка:** серия
`refactor/pipeline-stage-NN` (по одной-две стадии на PR)
**Требует:** 04 (golden E2E как страховка) — не начинать без него.

## Для чего апдейт

Главный структурный долг проекта. Один и тот же пайплайн выражен трижды:

- `hunter/apply_api.py` — 1039 строк (API-режим);
- `hunter/apply_cli.py` — 776 строк (CLI-режим), десятки комментариев
  «mirror of apply_api Step X»;
- `hunter/dual_apply.py` — 516 строк, третья копия оркестрации для shadow;
- `hunter/apply_shared.py` — 1879 строк / 47 функций, «shared»-свалка.

Каждая новая стадия (judge, verdict, refine, outreach, doomed gate — вся
история 2026-06/07) прошивалась **дважды или трижды**; почти каждый фикс в
Agent Work Log содержит «в обоих пайплайнах». Это налог на каждое будущее
изменение и постоянный риск «в CLI-ветке забыли».

Ключевое наблюдение: API и CLI различаются ровно **одной стадией** (как
получен первичный content.json — API-вызов vs Claude CLI) и **политикой
блокировки** (API — `sys.exit(0)`, CLI — delete-docs + return). Это два
параметра, а не два файла.

## Как именно будет происходить

### Целевая архитектура

```python
# hunter/pipeline/context.py
@dataclass
class ApplyContext:
    url: str; job_text: str = ""; content: dict = field(default_factory=dict)
    folder: Path | None = None; posting_lang: str = "EN"
    flags: PipelineFlags  # force, full, paste, permalink, shadow(=no side effects)
    outcome: Outcome | None = None  # EXPIRED / SKIP / BLOCKED / OK

# hunter/pipeline/runner.py
def run(stages: list[Stage], ctx: ApplyContext, policy: BlockPolicy) -> ApplyContext:
    for stage in stages:
        stage(ctx)
        if ctx.outcome in TERMINAL: break
    return ctx
```

Стадии — текущие «Step»-блоки, вынесенные в функции `(ctx) -> None` в пакет
`hunter/pipeline/stages/`: fetch, expired, doomed_gate, load_prompts,
generate (**единственная** различающаяся: `generate_api` / `generate_cli`),
ats_loop, sanitize, scrubs, claim_judge, lang_gate, qa, write_artifacts,
render_docs, pdf_verdict, refine_loop, tracker_stamps, outreach, deliver,
notify. Побочные эффекты (tracker/Telegram/Sheets) гейтятся флагом
`ctx.flags.shadow` — dual-apply перестаёт нуждаться в собственной оркестрации.

### Странглер-план (без big-bang)

1. **PR-0**: каркас (`context.py`, `runner.py`, пустой `stages/`) + golden
   E2E зелёный. Прод-поведение не тронуто.
2. **PR-1..N**: по 1–2 стадии за PR, с хвоста пайплайна к голове (outreach →
   refine → verdict → render → … → fetch). Хвост первым: он самый
   «зеркальный» (все fixes последних недель) и наименее ветвистый. Каждый PR:
   логика переезжает в стадию, `apply_api`/`apply_cli` вызывают её вместо
   инлайна, старый код удаляется, golden + юниты зелёные.
3. **PR-финал-1**: `apply_cli.main_cli` = `run(STAGES, ctx, policy=CLI)` —
   файл сжимается до entry + политика.
4. **PR-финал-2**: `dual_apply._generate_shadow` = тот же runner с
   `flags.shadow=True` + profile override. Удаление третьей копии.
5. **PR-финал-3**: разбор `apply_shared.py` по домам: `pipeline/gates.py`
   (lang, doomed), `pipeline/scrubs.py`, `notify.py`, `folders.py`. Реэкспорт
   из apply_shared на переходный период (как сделано с telegram_bot.py).

### Что НЕ делаем

- Никакого DSL/конфигурируемых-из-YAML пайплайнов — список стадий это
  обычный Python-список, порядок виден глазами.
- Никакой параллельности стадий — пайплайн принципиально последовательный.

## Что меняется в коде

| Файл | Изменение |
|------|-----------|
| `hunter/pipeline/` | **Новый пакет**: context, runner, stages/*, gates, scrubs |
| `hunter/apply_api.py` | 1039 → ~150 строк: entry, сборка ctx, `run(...)`, exit-политика |
| `hunter/apply_cli.py` | 776 → ~120 строк: то же с `generate_cli` |
| `hunter/dual_apply.py` | 516 → ~200 строк: остаётся detached-запуск/watchdog/Drive-upload, оркестрация уходит в runner |
| `hunter/apply_shared.py` | 1879 → реэкспорт-шим (потом удаление) |
| `tests/*` | Существующие юниты стадий перенацеливаются импортами (логика та же); golden E2E — без изменений ассертов (в этом его смысл) |
| `CLAUDE.md` | Architecture Overview + Pipeline Flow переписываются под стадии; Known Issues #3 закрывается |
| Попутно | `print()` внутри стадий → `logger` (214 вхождений в hunter/) — механическая замена в тех же PR |

## Критерий готовности

- `grep -rn "mirror of" hunter/` → 0.
- Новая стадия добавляется **одной** записью в список STAGES (проверить на
  фиктивной стадии в тесте).
- Golden E2E не менял ассерты ни в одном PR серии.
- dual-shadow использует runner (нет собственной последовательности вызовов).

## Риски

- Самый рискованный пункт роадмапа. Митигируется: страховка 04, PR-ы по 1–2
  стадии, хвост-вперёд, реэкспорт-шимы, немедленный прод-мониторинг после
  каждого мержа (первый auto-apply цикл).
- Скрытые различия «зеркал» (CLI-ветка местами отстаёт от API намеренно —
  напр., refine skip без LLM_API_KEY): каждое такое различие фиксируется
  явным полем политики, а не теряется — инвентаризация различий = первый шаг
  PR-0.
