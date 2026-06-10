"""
hunter/lang_guard.py — Language routing + contamination guard.

Two jobs:

  1. `detect_posting_language(job_text)` — decide PL vs EN for a job posting
     deterministically (Polish-token density), so the apply pipeline can route by
     language instead of trusting the LLM's self-reported `lang` field.

  2. Contamination detection — find Polish fragments that leaked into the English
     resume / cover-letter fields (and stray English prose in Polish fields). The
     apply enforce-gate uses this to trigger a targeted re-translation pass and to
     BLOCK delivery of a contaminated document instead of shipping a broken PDF.

Why a custom detector instead of `langdetect`: the contaminated fields are short,
keyword-dense strings ("Angular, TypeScript, Mikroserwisach Architecture") where
statistical language ID is unreliable. Polish has highly distinctive diacritics
and inflectional morphology, so a lexicon + suffix + diacritic detector is both
more precise on this input and dependency-free.

Signal strength:
  - STRONG  (diacritics, Polish lexicon word, bilingual gloss): high confidence —
            enough to BLOCK an English document if it survives a repair pass.
  - SOFT    (Polish inflectional suffix on a non-tech token): enough to TRIGGER a
            repair pass, but never to block on its own (avoids false positives on
            the rare English word with a Polish-looking ending).
"""

from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Tech / proper-noun allowlist — tokens that look foreign but are language-neutral
# and must never be flagged. Kept broad on purpose; matched case-insensitively.
# ---------------------------------------------------------------------------
_TECH_TERMS = {
    # frameworks / libs / langs
    "angular", "angularjs", "react", "reactjs", "vue", "vuejs", "svelte",
    "typescript", "javascript", "node", "nodejs", "nestjs", "nextjs", "next",
    "rxjs", "ngrx", "nx", "redux", "signals", "jquery", "bootstrap", "tailwind",
    "scss", "sass", "css", "css3", "html", "html5", "json", "xml", "yaml",
    "python", "php", "java", "kotlin", "golang", "rust", "ruby", "rails",
    "django", "flask", "fastapi", "spring", "dotnet", "graphql", "rest",
    "restful", "grpc", "websocket", "webpack", "vite", "rollup", "babel",
    # testing
    "jest", "jasmine", "karma", "cypress", "playwright", "mocha", "chai",
    "selenium", "sonarqube", "vitest",
    # tooling / devops / cloud
    "git", "github", "gitlab", "jenkins", "docker", "kubernetes", "k8s",
    "openshift", "terraform", "ansible", "aws", "azure", "gcp", "ci", "cd",
    "cicd", "devops", "agile", "scrum", "safe", "kanban", "jira", "figma",
    "storybook", "npm", "pnpm", "yarn", "eslint", "prettier",
    "copilot", "cursor", "claude", "anthropic", "openai", "llm", "openvino",
    "ag", "grid", "aggrid", "pwa", "spa", "ssr", "ssg", "seo", "wcag",
    "api", "apis", "sdk", "ui", "ux", "sql", "nosql", "mongodb", "postgresql",
    "postgres", "mysql", "redis", "kafka", "rabbitmq", "elasticsearch",
    "intel", "atruvia", "fairmarkit", "altoros", "solbegsoft", "staronka",
    "alten", "sii", "venture", "labs", "opus", "haiku", "sonnet",
}

# Proper nouns — place names that legitimately carry Polish diacritics but are NOT
# contamination when they appear in an English CV (the candidate lives in Wrocław).
# Both diacritic and ASCII forms; matched case-insensitively as whole tokens.
_PROPER_NOUNS = {
    "wrocław", "wroclaw", "wrocławiu", "wroclawiu",
    "kraków", "krakow", "krakowie", "krakowiaków", "krakowiakow",
    "warszawa", "warszawie", "warszawy", "warsaw",
    "poznań", "poznan", "poznaniu", "gdańsk", "gdansk", "gdańsku",
    "łódź", "lodz", "łodzi",
}

# Strong-signal Polish lexicon: function words + recurring job-posting stems that
# carry no diacritics (so the diacritic check alone would miss them). Whole-word.
_PL_LEXICON = {
    # function words
    "się", "sie", "oraz", "przez", "który", "ktora", "które", "ktore", "która",
    "dla", "lat", "lub", "albo", "jako", "jest", "być", "byc", "praca", "pracy",
    "firma", "firmy", "nasz", "nasza", "nasze", "jego", "tego", "tych", "temu",
    "czy", "już", "juz", "bardzo", "także", "takze", "również", "rowniez",
    # job-posting stems (diacritic-free forms)
    "doświadczenie", "doswiadczenie", "doświadczenia", "doswiadczenia",
    "umiejętności", "umiejetnosci", "wymagania", "wymagań", "wymagan",
    "obowiązki", "obowiazki", "znajomość", "znajomosc", "wiedza", "wiedzy",
    "rozwój", "rozwoj", "rozwiązań", "rozwiazan", "rozwiązania", "rozwiazania",
    "zespół", "zespol", "zespole", "zespołem", "zespolem", "zespołu", "zespolu",
    "projekt", "projekty", "projektów", "projektow", "projektach",
    "aplikacji", "aplikacja", "aplikacje", "interfejs", "interfejsy",
    "interfejsów", "interfejsow", "responsywne", "responsywny", "responsywnych",
    "wdrożenie", "wdrozenie", "wdrożeń", "wdrozen", "testowanie", "testów",
    "testow", "jednostkowych", "danych", "bazy", "baza", "dokumentacji",
    "dokumentacja", "technicznej", "techniczna", "integracja", "integracji",
    "wersji", "kontroli", "systemu", "monolitycznych", "mikroserwisach",
    "mikrofrontendów", "mikrofrontendow", "skalowalności", "skalowalnosci",
    "utrzymywalności", "utrzymywalnosci", "wydajności", "wydajnosci",
    "jakość", "jakosc", "jakości", "jakosci", "housowe", "wewnętrzne",
    "wewnetrzne", "narzędzi", "narzedzi", "narzędzia", "narzedzia",
}

# Polish inflectional suffixes (SOFT signal). Matched on alphabetic tokens that are
# not in the tech allowlist. Kept deliberately conservative — endings that collide
# with common English words (-ach → approach/coach/teach, -lem → problem/emblem,
# -ego → ego/lego, -ami → miami/salami) are EXCLUDED so they never false-positive.
_PL_SUFFIXES = (
    "ość", "ości", "osci", "owych", "owym", "owej", "owski",
    "ych", "nych", "acji", "acja", "enie", "nej", "iej",
    "łem", "łam", "łeś", "owanie", "ujemy", "ować",
)

_PL_DIACRITICS_RE = re.compile(r"[ąćęłńóśźż]", re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-zÀ-ÿąćęłńóśźżĄĆĘŁŃÓŚŹŻ][\w\-]*", re.UNICODE)

# Bilingual gloss: "<word> (<word(s)>)" where one side is Polish — e.g.
# "responsywne interfejsy (responsive interfaces)" or "API integration (integracja z API)".
_GLOSS_RE = re.compile(r"([^(),;|]+?)\s*\(([^()]+?)\)")

# English prose markers used to catch stray English sentences inside _pl fields.
_EN_PROSE_WORDS = {
    "the", "and", "with", "for", "from", "this", "that", "have", "has",
    "experience", "development", "developer", "years", "team", "teams",
    "across", "building", "delivered", "built", "led", "designed", "skills",
    "responsible", "including", "while", "which", "their", "they", "your",
}


def _norm(tok: str) -> str:
    return tok.lower().strip(" .,;:|/()-")


def _is_tech(tok: str) -> bool:
    n = _norm(tok)
    if not n:
        return True
    if n in _TECH_TERMS or n in _PROPER_NOUNS:
        return True
    # acronym / version token (ALLCAPS, has a digit, or <=2 chars) → language-neutral
    if any(ch.isdigit() for ch in n):
        return True
    if len(n) <= 2:
        return True
    if tok.isupper():
        return True
    return False


def _looks_polish_word(tok: str, *, soft: bool) -> bool:
    """True if `tok` is a Polish word. `soft=True` also accepts suffix matches."""
    if _is_tech(tok):
        return False
    n = _norm(tok)
    if not n:
        return False
    if _PL_DIACRITICS_RE.search(n):
        return True
    if n in _PL_LEXICON:
        return True
    if soft:
        for suf in _PL_SUFFIXES:
            if n.endswith(suf) and len(n) > len(suf) + 1:
                return True
    return False


def polish_fragments(text: str, *, soft: bool = True) -> list[str]:
    """Return Polish word/gloss fragments found in `text` (deduped, in order).

    `soft=False` restricts to STRONG signals (diacritics + lexicon + gloss),
    suitable for a block decision. `soft=True` also includes suffix-heuristic
    matches, suitable for triggering a repair pass.
    """
    if not text or not isinstance(text, str):
        return []
    hits: list[str] = []
    seen: set[str] = set()

    # Bilingual gloss: flag the whole "X (Y)" when either side carries Polish.
    for m in _GLOSS_RE.finditer(text):
        left, right = m.group(1), m.group(2)
        if any(_looks_polish_word(w, soft=False) for w in _WORD_RE.findall(left)) or any(
            _looks_polish_word(w, soft=False) for w in _WORD_RE.findall(right)
        ):
            frag = m.group(0).strip()
            key = frag.lower()
            if key not in seen:
                seen.add(key)
                hits.append(frag)

    for tok in _WORD_RE.findall(text):
        if _looks_polish_word(tok, soft=soft):
            key = tok.lower()
            if key not in seen:
                seen.add(key)
                hits.append(tok)
    return hits


def english_prose_fragments(text: str) -> list[str]:
    """Return English prose words found in `text` (for guarding _pl fields).

    Polish IT writing legitimately borrows English tech terms, so we only flag
    English *function/prose* words (the, and, with, experience...) — not tech.
    """
    if not text or not isinstance(text, str):
        return []
    hits: list[str] = []
    seen: set[str] = set()
    for tok in _WORD_RE.findall(text):
        n = _norm(tok)
        if n in _EN_PROSE_WORDS and n not in seen:
            seen.add(n)
            hits.append(tok)
    return hits


def detect_posting_language(job_text: str) -> str:
    """Return "PL" or "EN" for a job posting, by Polish-token density.

    Deterministic and dependency-free. A posting is PL when a meaningful share of
    its non-tech words are Polish (strong signals only). Defaults to EN on empty /
    ambiguous input.
    """
    if not job_text or not isinstance(job_text, str):
        return "EN"
    # Density of Polish *content* words. Tech terms and Polish place names are in the
    # allowlist (_is_tech) and excluded, so an English posting for a Wrocław/Kraków
    # office is not misread as Polish just because the city name carries diacritics.
    # _looks_polish_word already counts diacritics + lexicon for non-allowlist words.
    words = [w for w in _WORD_RE.findall(job_text) if not _is_tech(w)]
    if len(words) < 10:
        return "EN"
    pl = sum(1 for w in words if _looks_polish_word(w, soft=False))
    return "PL" if (pl / len(words)) >= 0.08 else "EN"


# ---------------------------------------------------------------------------
# Content-level scan
# ---------------------------------------------------------------------------

def _iter_en_strings(content: dict):
    """Yield (path, text) for every English-expected string field in content."""
    for key in ("cover_letter_en", "about_me_en"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            yield key, val
    resume = content.get("resume_en")
    if isinstance(resume, dict):
        yield from _iter_resume_strings("resume_en", resume)


def _iter_pl_strings(content: dict):
    for key in ("cover_letter_pl", "about_me_pl"):
        val = content.get(key)
        if isinstance(val, str) and val.strip():
            yield key, val
    resume = content.get("resume_pl")
    if isinstance(resume, dict):
        yield from _iter_resume_strings("resume_pl", resume)


def _iter_resume_strings(prefix: str, resume: dict):
    if isinstance(resume.get("summary"), str):
        yield f"{prefix}.summary", resume["summary"]
    skills = resume.get("skills")
    if isinstance(skills, dict):
        for sk, sv in skills.items():
            if sk == "languages":  # "Polish (B2)" / "Polski (B2)" are expected
                continue
            if isinstance(sv, str):
                yield f"{prefix}.skills.{sk}", sv
    for i, entry in enumerate(resume.get("experience") or []):
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("stack_line"), str):
            yield f"{prefix}.experience[{i}].stack_line", entry["stack_line"]
        for j, bullet in enumerate(entry.get("bullets") or []):
            if isinstance(bullet, str):
                yield f"{prefix}.experience[{i}].bullets[{j}]", bullet


def scan_content(content: dict) -> dict:
    """Scan a full content dict for language contamination.

    Returns a dict:
        {
          "en_strong": {path: [frags]},   # Polish (strong) in English fields → block-worthy
          "en_soft":   {path: [frags]},   # Polish (soft) in English fields   → repair-worthy
          "pl_english":{path: [frags]},   # English prose in Polish fields     → repair-worthy
        }
    Only contaminated paths are included.
    """
    en_strong: dict[str, list[str]] = {}
    en_soft: dict[str, list[str]] = {}
    pl_english: dict[str, list[str]] = {}

    for path, text in _iter_en_strings(content):
        strong = polish_fragments(text, soft=False)
        if strong:
            en_strong[path] = strong
        soft = polish_fragments(text, soft=True)
        # soft-only (not already strong) fragments
        soft_only = [f for f in soft if f not in strong]
        if soft_only:
            en_soft[path] = soft_only

    for path, text in _iter_pl_strings(content):
        eng = english_prose_fragments(text)
        if len(eng) >= 3:  # a few stray anglicisms are fine; prose = contamination
            pl_english[path] = eng

    return {"en_strong": en_strong, "en_soft": en_soft, "pl_english": pl_english}


def has_blocking_contamination(scan: dict) -> bool:
    """True when strong Polish leaked into English fields (block delivery)."""
    return bool(scan.get("en_strong"))


def needs_repair(scan: dict) -> bool:
    """True when any contamination (strong/soft/pl) warrants a repair pass."""
    return bool(scan.get("en_strong") or scan.get("en_soft") or scan.get("pl_english"))
