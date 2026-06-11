"""Tests for the prestige-claim scrub and skills slash-gloss dedup.

Both fixtures are real production cases:
  - PeopleVibe (2026-06-11): "Fortune 500 clients" fabricated into EN + PL
    summaries while the posting never mentions it.
  - Shimi (2026-06-10): "term / synonym" gloss pairs in skills.methodologies
    left by the ATS keyword mirroring.
"""

from hunter.apply_shared import (
    _collapse_gloss_item,
    _dedup_skill_glosses,
    _split_skill_items,
    _strip_prestige_claims,
)

# ---------------------------------------------------------------------------
# Prestige scrub
# ---------------------------------------------------------------------------

_EN_SUMMARY = (
    "Senior Frontend Developer with 10+ years of commercial experience "
    "specializing in Angular, TypeScript, and RxJS. Built scalable enterprise "
    "applications for 300+ German banks and Fortune 500 clients, with proven "
    "expertise in banking sector projects."
)
_PL_SUMMARY = (
    "Senior Frontend Developer z 10+ latami doświadczenia. Budował aplikacje "
    "enterprise dla 300+ niemieckich banków i klientów Fortune 500, z "
    "udokumentowaną ekspertyzą w projektach sektora bankowego."
)


def _content(en_summary: str = _EN_SUMMARY, pl_summary: str = _PL_SUMMARY) -> dict:
    return {
        "resume_en": {"summary": en_summary, "skills": {}, "experience": []},
        "resume_pl": {"summary": pl_summary, "skills": {}, "experience": []},
    }


def test_prestige_removed_from_en_summary_keeps_banks() -> None:
    out, fixes = _strip_prestige_claims(_content(), job_text="We build Angular apps.")
    summary = out["resume_en"]["summary"]
    assert "Fortune 500" not in summary
    assert "300+ German banks" in summary
    assert "with proven expertise in banking sector projects" in summary
    assert any("EN" in f for f in fixes)


def test_prestige_removed_from_pl_summary() -> None:
    out, fixes = _strip_prestige_claims(_content(), job_text="")
    summary = out["resume_pl"]["summary"]
    assert "Fortune 500" not in summary
    assert "300+ niemieckich banków" in summary
    assert any("PL" in f for f in fixes)


def test_prestige_kept_when_posting_mentions_it() -> None:
    job_text = "Our client serves Fortune 500 companies across Europe."
    out, fixes = _strip_prestige_claims(_content(), job_text=job_text)
    assert "Fortune 500" in out["resume_en"]["summary"]
    assert fixes == []


def test_clean_content_untouched() -> None:
    content = _content(
        en_summary="Senior Frontend Developer with 10+ years of experience.",
        pl_summary="Senior Frontend Developer z 10+ latami doświadczenia.",
    )
    out, fixes = _strip_prestige_claims(content, job_text="")
    assert fixes == []
    assert "10+ years" in out["resume_en"]["summary"]


def test_prestige_sentence_dropped_when_clause_scrub_cannot_reach() -> None:
    content = _content(
        en_summary=(
            "Fortune 500 clients trusted his delivery. "
            "Senior Frontend Developer with 10+ years of experience."
        )
    )
    out, _ = _strip_prestige_claims(content, job_text="")
    summary = out["resume_en"]["summary"]
    assert "Fortune 500" not in summary
    assert "10+ years" in summary


def test_prestige_removed_from_skills_and_bullets() -> None:
    content = {
        "resume_en": {
            "summary": "Clean summary.",
            "skills": {
                "methodologies": "Agile (Scrum, SAFe), Fortune 500 delivery, Code Reviews",
            },
            "experience": [
                {
                    "company": "Altoros",
                    "bullets": [
                        "Delivered dashboards for top-tier clients and internal teams",
                        "Migrated the platform to Angular 15",
                    ],
                }
            ],
        },
    }
    out, fixes = _strip_prestige_claims(content, job_text="")
    skills = out["resume_en"]["skills"]["methodologies"]
    assert "Fortune 500" not in skills
    assert "Agile (Scrum, SAFe)" in skills and "Code Reviews" in skills
    bullets = out["resume_en"]["experience"][0]["bullets"]
    assert all("top-tier" not in b for b in bullets)
    assert "Migrated the platform to Angular 15" in bullets
    assert len(fixes) == 2


def test_prestige_only_bullet_dropped_entirely() -> None:
    content = {
        "resume_en": {
            "summary": "Clean.",
            "skills": {},
            "experience": [
                {"company": "SII", "bullets": ["Served Fortune 500 clients."]},
            ],
        },
    }
    out, _ = _strip_prestige_claims(content, job_text="")
    assert out["resume_en"]["experience"][0]["bullets"] == []


def test_prestige_scrub_about_me() -> None:
    content = {
        "about_me_en": (
            "I am a frontend developer. I built apps for banks and Fortune 500 "
            "clients across Europe."
        ),
    }
    out, fixes = _strip_prestige_claims(content, job_text="")
    assert "Fortune 500" not in out["about_me_en"]
    assert "banks" in out["about_me_en"]
    assert fixes


# ---------------------------------------------------------------------------
# Slash-gloss dedup
# ---------------------------------------------------------------------------

# Real Shimi methodologies string (production output, 2026-06-10).
_SHIMI_METHODOLOGIES = (
    "Frontend Architecture, Security by Design / Security best practices, "
    "Agile (Scrum, SAFe), Code Reviews, "
    "Performance Optimization / Performance optimisation, Scalability, "
    "Maintainability, CI/CD, UI/UX Design, "
    "technical documentation / High-quality technical documentation, "
    "functional requirements validation / defining and validating functional requirements"
)


def test_split_skill_items_respects_parens() -> None:
    items = _split_skill_items("Agile (Scrum, SAFe), Code Reviews")
    assert items == ["Agile (Scrum, SAFe)", "Code Reviews"]


def test_gloss_uk_us_spelling_collapsed() -> None:
    assert (
        _collapse_gloss_item("Performance Optimization / Performance optimisation")
        == "Performance Optimization"
    )


def test_gloss_substring_collapsed_keeps_first() -> None:
    assert (
        _collapse_gloss_item(
            "technical documentation / High-quality technical documentation"
        )
        == "technical documentation"
    )


def test_gloss_rephrasing_collapsed() -> None:
    assert (
        _collapse_gloss_item(
            "functional requirements validation / "
            "defining and validating functional requirements"
        )
        == "functional requirements validation"
    )


def test_distinct_skills_with_slash_kept() -> None:
    assert (
        _collapse_gloss_item("OpenShift / container platforms")
        == "OpenShift / container platforms"
    )


def test_compact_slash_untouched() -> None:
    assert _collapse_gloss_item("UI/UX Design") == "UI/UX Design"
    assert _collapse_gloss_item("CI/CD") == "CI/CD"


def test_dedup_shimi_methodologies_end_to_end() -> None:
    content = {
        "resume_en": {
            "skills": {"methodologies": _SHIMI_METHODOLOGIES, "languages": "English (Fluent)"},
        },
    }
    out, fixes = _dedup_skill_glosses(content)
    meth = out["resume_en"]["skills"]["methodologies"]
    assert "Performance optimisation" not in meth
    assert "Performance Optimization" in meth
    assert "High-quality technical documentation" not in meth
    assert "technical documentation" in meth
    assert "defining and validating" not in meth
    assert "functional requirements validation" in meth
    # Genuinely different sides survive
    assert "Security by Design / Security best practices" in meth
    # Untouched neighbours
    assert "Agile (Scrum, SAFe)" in meth
    assert "CI/CD" in meth and "UI/UX Design" in meth
    assert fixes == ["[EN] collapsed gloss pair(s) in skills.methodologies"]


def test_dedup_handles_list_valued_skills() -> None:
    content = {
        "resume_pl": {
            "skills": {
                "methodologies": [
                    "Optymalizacja wydajności / Optymalizacja wydajnosci",
                    "Code Reviews",
                ]
            },
        },
    }
    out, fixes = _dedup_skill_glosses(content)
    assert out["resume_pl"]["skills"]["methodologies"][0] == "Optymalizacja wydajności"
    assert fixes


def test_dedup_clean_skills_no_fixes() -> None:
    content = {
        "resume_en": {"skills": {"frontend": "Angular (2-22), TypeScript, RxJS"}},
    }
    out, fixes = _dedup_skill_glosses(content)
    assert fixes == []
    assert out["resume_en"]["skills"]["frontend"] == "Angular (2-22), TypeScript, RxJS"
