# GDPR / RODO Consent Clause on CV — Plan

Branch: `feat/gdpr-consent-clause`

## Problem

Generated CVs have **no GDPR/RODO consent clause**. On the Polish market (pracuj.pl,
theprotocol, justjoin, nofluffjobs — the primary job flow) a data-processing consent
clause at the bottom of the CV is the norm. Without it some recruiters/ATS cannot
legally store the candidate's data, and some auto-reject. For international/remote
roles the clause is unnecessary and slightly out of place.

Confirmed absent in: `prompts/candidate_profile.md`, all `prompts/base_cv_*.md`,
and the `generate_docs.py` resume renderer (CV ends at ADDITIONAL COURSES).

## Decision

- The clause is a **static legal string rendered in `generate_docs.py`**, NOT
  LLM-generated. Reasons: it must never vary, never be omitted, never consume LLM
  tokens, and not be at risk of the model "tailoring" legal text.
- **Body, not footer.** Render it as the **last paragraph in the document body**
  (bottom of the last page), after ADDITIONAL COURSES: small font (~7.5pt), italic,
  grey, left-aligned. NOT in a docx footer/колонтитул.
  - **Why not the footer:** most ATS parsers extract only the main document stream
    and skip header/footer text — exactly the visibility we need. Footers in
    python-docx also repeat on every page by default; "last page only" needs
    section juggling + "different first page" and can shift under LibreOffice PDF
    conversion. Body-bottom is both the Polish-CV standard and the ATS-safe choice,
    and visually identical (bottom of the last page).
- **PL CV** gets the Polish clause; **EN CV** gets the English clause.
  Default: include on both (EN clause is short and harmless on EU applications).
  A config/env toggle lets us restrict to PL-only if desired.

## Clause text (final — current + future recruitment; full PL Act + GDPR Art. 6(1)(a))

EN wording sourced from Polish-recruitment guides (careersinpoland, cvszablony 2026),
not a raw translation: precise legal basis (Art. 6(1)(a) = consent), official Dz.U.
reference, standard "current and future recruitment" scope. PL mirrors it.

PL:
> Wyrażam zgodę na przetwarzanie danych osobowych zawartych w niniejszym dokumencie
> do realizacji obecnego oraz przyszłych procesów rekrutacji zgodnie z ustawą z dnia
> 10 maja 2018 roku o ochronie danych osobowych (Dz.U. 2018 poz. 1000) oraz zgodnie
> z art. 6 ust. 1 lit. a RODO (Rozporządzenie Parlamentu Europejskiego i Rady (UE)
> 2016/679 z dnia 27 kwietnia 2016 r.).

EN:
> I hereby consent to the processing of my personal data for the purposes necessary
> to carry out the current and future recruitment processes, in accordance with the
> Act of 10 May 2018 on the Protection of Personal Data (Journal of Laws 2018, item
> 1000) and Article 6(1)(a) of Regulation (EU) 2016/679 of the European Parliament
> and of the Council of 27 April 2016 (GDPR).

## Implementation steps

1. **`generate_docs.py`**
   - Add module-level constants `GDPR_CLAUSE_PL` / `GDPR_CLAUSE_EN`.
   - Add helper `add_gdpr_clause(doc, lang)` that appends the italic grey footer
     paragraph (small font, e.g. `set_font(run, size=7.5, italic=True, color=(120,120,120))`).
   - `build_resume(doc, data, stack)` → add a `lang` param (`build_resume(doc, data, stack, lang)`);
     call `add_gdpr_clause(doc, lang)` at the end of the function.
   - Update the two `build_resume(...)` call sites in `main()` to pass `"EN"` / `"PL"`.
   - Gate on an env toggle `CV_GDPR_CLAUSE` (default `"both"`; accepts `both` / `pl` / `none`)
     read from `hunter.config`.

2. **`hunter/config.py`**
   - Add `CV_GDPR_CLAUSE = os.getenv("CV_GDPR_CLAUSE", "both").strip().lower()`.

3. **Docstring** at top of `generate_docs.py` — note the clause is auto-appended
   (so future readers don't add it to prompts/profile).

4. **Tests** (`tests/test_generate_docs*.py` or new `tests/test_gdpr_clause.py`)
   - EN CV contains `GDPR`, not `RODO`.
   - PL CV contains `RODO`.
   - `CV_GDPR_CLAUSE=none` → neither clause present.
   - `CV_GDPR_CLAUSE=pl` → PL CV has clause, EN CV does not.
   - Clause is the last paragraph and renders at the configured small font.

5. **Page-fit check** — clause is one short line; confirm 2-page limit still holds
   on a representative sample via `tools/preview_apply.py` (visual, EN+PL).

6. **CLAUDE.md** — add a one-line note under the apply pipeline / generate_docs
   section that the GDPR clause is appended at render time, controlled by
   `CV_GDPR_CLAUSE`; add the env var to the config table.

## Out of scope

- No change to cover letters (clause belongs on the CV only).
- No LLM prompt changes (`generation_rules.md` already correctly tells the model
  NOT to absorb company GDPR/RODO compliance into skills — unrelated, leave as is).

## Verification

- `python -m compileall generate_docs.py hunter/config.py`
- `pytest tests/ -k gdpr` and full `pytest tests/`
- Visual: `python tools/preview_apply.py` on one PL fixture, confirm clause renders
  at the very bottom in small italic grey and CV stays within 2 pages.
