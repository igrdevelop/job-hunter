"""Tests for hunter.lang_guard — language routing + contamination detection.

Real-world contamination samples are taken from two production failures where a
Polish posting produced an English CV peppered with Polish keywords:
  - RTVEuroAGD (theprotocol.it)
  - DCG (solid.jobs)
"""

from hunter import lang_guard as lg


# ---------------------------------------------------------------------------
# detect_posting_language
# ---------------------------------------------------------------------------


def test_detect_language_english_posting():
    txt = (
        "We are looking for a Senior Frontend Developer with strong Angular and "
        "TypeScript skills. You will build scalable applications, write unit tests, "
        "and collaborate with cross-functional teams in an Agile environment."
    )
    assert lg.detect_posting_language(txt) == "EN"


def test_detect_language_polish_posting():
    txt = (
        "Poszukujemy programisty Frontend ze znajomością Angulara i TypeScript. "
        "Wymagane doświadczenie w budowaniu responsywnych interfejsów oraz pisaniu "
        "testów jednostkowych. Praca w zespole zwinnym, projekty wewnętrzne."
    )
    assert lg.detect_posting_language(txt) == "PL"


def test_detect_language_polish_without_diacritics():
    txt = (
        "Poszukujemy programisty frontend ze znajomoscia Angulara. Wymagania: "
        "doswiadczenie w aplikacjach webowych, testowanie jednostkowych modulow, "
        "praca w zespole, projekty wewnetrzne oraz rozwoj umiejetnosci."
    )
    assert lg.detect_posting_language(txt) == "PL"


def test_detect_language_english_posting_with_polish_city_names():
    # English posting for a Polish-office role: the city names carry diacritics but
    # must NOT flip detection to PL (regression: raw diacritic count >= 3 short-circuit).
    txt = (
        "We are hiring a Senior Frontend Developer for our offices in Wrocław, "
        "Kraków and Łódź. You will build Angular applications, write unit tests, and "
        "collaborate with cross-functional teams. Hybrid work from the Wrocław hub. "
        "Strong TypeScript and RxJS skills required; relocation to Poland supported."
    )
    assert lg.detect_posting_language(txt) == "EN"


def test_detect_language_empty_defaults_en():
    assert lg.detect_posting_language("") == "EN"
    assert lg.detect_posting_language(None) == "EN"


def test_detect_language_short_text_defaults_en():
    assert lg.detect_posting_language("Angular React TypeScript") == "EN"


# ---------------------------------------------------------------------------
# Polish contamination in English fields — strong signals
# ---------------------------------------------------------------------------


def test_diacritic_word_is_strong():
    assert lg.polish_fragments("10+ years of experience (7+ lat doświadczenia)", soft=False)


def test_bilingual_gloss_is_strong():
    frags = lg.polish_fragments("responsywne interfejsy (responsive interfaces)", soft=False)
    assert frags
    assert any("responsywne" in f.lower() for f in frags)


def test_lexicon_word_without_diacritics_is_strong():
    # "mikroserwisach", "monolitycznych" have no diacritics but are in the lexicon
    frags = lg.polish_fragments(
        "SPA transformation from monolitycznych to mikroserwisach architectures",
        soft=False,
    )
    assert any("mikroserwisach" in f.lower() for f in frags)


def test_real_skills_string_flagged():
    skills = (
        "Angular (2-22), TypeScript, responsywne interfejsy (responsive interfaces), "
        "Mikroserwisach Architecture / Microservices Architecture, "
        "Dokumentacji technicznej creation tools"
    )
    assert lg.polish_fragments(skills, soft=False)


def test_clean_english_skills_not_flagged():
    skills = (
        "Angular (2-22), Nx Monorepo, NgRx, Signals, RxJS, AG Grid, TypeScript, "
        "JavaScript, HTML5, Bootstrap, SCSS, Microservices Architecture, "
        "Responsive interfaces, Technical documentation, REST APIs"
    )
    assert lg.polish_fragments(skills, soft=False) == []


def test_clean_english_summary_not_flagged():
    summary = (
        "Senior Frontend Developer with 10+ years of expertise delivering scalable "
        "Angular applications for enterprise clients. Specialized in Angular, "
        "TypeScript and comprehensive testing strategies across e-commerce, fintech "
        "and healthcare domains, serving millions of users."
    )
    assert lg.polish_fragments(summary, soft=False) == []
    assert lg.polish_fragments(summary, soft=True) == []


def test_tech_terms_never_flagged():
    # Jest looks like Polish "jest" but is a testing framework; NgRx/RxJS etc.
    assert lg.polish_fragments("Jest, Jasmine, NgRx, RxJS, Cypress, SonarQube", soft=True) == []


def test_common_english_words_not_soft_flagged():
    # Suffix heuristic must not collide with everyday English (regression: "approach"
    # / "problem" were falsely flagged by the -ach / -lem suffixes).
    txt = (
        "My approach to problem solving and research helps each team reach its goals. "
        "I coach engineers and teach best practices through code review."
    )
    assert lg.polish_fragments(txt, soft=True) == []


def test_polish_city_names_not_flagged():
    # Place names carry diacritics but are legitimate in an English CV (regression:
    # the gate once blocked an entire delivery over the city "Wrocław").
    txt = (
        "Senior Frontend Developer based in Wrocław, available for hybrid work in "
        "Kraków and Warsaw. Delivered banking projects for clients across Poland."
    )
    assert lg.polish_fragments(txt, soft=False) == []
    assert lg.polish_fragments(txt, soft=True) == []


def test_city_among_real_contamination_still_flags_the_polish():
    txt = "Available in Wrocław; built responsywne interfejsy for the client."
    frags = lg.polish_fragments(txt, soft=False)
    assert any("responsywne" in f.lower() for f in frags)
    assert not any("wroc" in f.lower() for f in frags)


# ---------------------------------------------------------------------------
# English prose in Polish fields
# ---------------------------------------------------------------------------


def test_english_prose_in_polish_field_detected():
    txt = "Senior Frontend Developer with 10 years of experience building teams"
    assert len(lg.english_prose_fragments(txt)) >= 3


def test_polish_field_with_anglicisms_ok():
    txt = (
        "Senior Frontend Developer z 10-letnim doświadczeniem w Angular i TypeScript. "
        "Specjalizuję się w architekturze frontend, code review oraz wdrożeniach CI/CD."
    )
    # 'frontend', 'code', 'review' are accepted anglicisms, not prose markers
    assert len(lg.english_prose_fragments(txt)) < 3


# ---------------------------------------------------------------------------
# scan_content
# ---------------------------------------------------------------------------


def _contaminated_content():
    return {
        "resume_en": {
            "summary": "Senior Frontend Developer with 10+ years (7+ lat doświadczenia).",
            "skills": {
                "frontend": "Angular, responsywne interfejsy (responsive interfaces)",
                "languages": "English (Fluent), Polish (B2)",
            },
            "experience": [
                {
                    "stack_line": "Stack: Angular, TypeScript",
                    "bullets": ["Built apps on projekty in-housowe (in-house projects)"],
                }
            ],
        },
        "cover_letter_en": "Dear Hiring Manager, I am writing to apply for the role.",
    }


def test_scan_flags_strong_contamination():
    scan = lg.scan_content(_contaminated_content())
    assert lg.has_blocking_contamination(scan)
    assert "resume_en.summary" in scan["en_strong"]
    assert "resume_en.skills.frontend" in scan["en_strong"]


def test_scan_ignores_languages_skill_field():
    scan = lg.scan_content(_contaminated_content())
    # "Polish (B2)" in skills.languages must not be flagged
    assert "resume_en.skills.languages" not in scan["en_strong"]
    assert "resume_en.skills.languages" not in scan.get("en_soft", {})


def test_scan_clean_content_no_contamination():
    clean = {
        "resume_en": {
            "summary": "Senior Frontend Developer with 10+ years of Angular expertise.",
            "skills": {"frontend": "Angular, TypeScript, RxJS", "languages": "English (Fluent)"},
            "experience": [{"stack_line": "Stack: Angular, TypeScript", "bullets": ["Built apps"]}],
        },
        "cover_letter_en": "Dear Hiring Manager, I am writing to apply.",
    }
    scan = lg.scan_content(clean)
    assert not lg.has_blocking_contamination(scan)
    assert not lg.needs_repair(scan)


def test_scan_cover_letter_en_polish_flagged():
    content = {
        "resume_en": {"summary": "Clean English summary about Angular work."},
        "cover_letter_en": "Dear Hiring Manager, posiadam duże doświadczenie w Angular.",
    }
    scan = lg.scan_content(content)
    assert "cover_letter_en" in scan["en_strong"]
