"""
Tests for the doomed-vacancy gate (docs/DOOMED_GATE_PLAN.md, milestone M1):
GateFinding + assess_job_text + all HARD/SOFT rule families and their vetoes,
plus the screen_job_text() backward-compat contract and the recommendation-tail
/ theprotocol.it SEO-footer contamination strip.

Fixtures under tests/fixtures/doomed_gate/ (see the NOTE header in each file
for provenance): bigbearai_job_posting.txt and megaport_job_posting.txt are
reconstructions of the two real cases the plan cites (originals not
recoverable from this dev machine — see file headers), fairmarkit_job_posting.txt
is the exact real LinkedIn dump that surfaced the "Similar jobs" contamination
bug during M4 calibration.
"""

from pathlib import Path

import pytest

from hunter.filters import (
    GateFinding,
    assess_job_text,
    screen_job_text,
    _strip_recommendation_tail,
)

FIXTURES = Path(__file__).parent / "fixtures" / "doomed_gate"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _rules(findings: list[GateFinding]) -> set[str]:
    return {f.rule for f in findings}


# ── GateFinding dataclass ──────────────────────────────────────────────────


def test_gate_finding_is_frozen_dataclass() -> None:
    f = GateFinding(rule="x", severity="hard", evidence="y")
    assert f.rule == "x" and f.severity == "hard" and f.evidence == "y"
    with pytest.raises(AttributeError):
        f.rule = "z"  # type: ignore[misc]


# ── Real-world fixture cases (docs/DOOMED_GATE_PLAN.md acceptance criteria) ──


def test_bigbearai_caught_by_foreign_onsite_hard_rule() -> None:
    text = _read("bigbearai_job_posting.txt")
    findings = assess_job_text(text, title="Senior Front End Angular Engineer", company="BigbearAI")
    hard = [f for f in findings if f.severity == "hard"]
    assert any(f.rule == "foreign_onsite_hybrid" for f in hard)


def test_megaport_caught_by_stack_mismatch_soft_rule() -> None:
    text = _read("megaport_job_posting.txt")
    findings = assess_job_text(text, title="Senior Frontend Software Engineer", company="Megaport")
    soft = [f for f in findings if f.severity == "soft"]
    assert any(f.rule == "stack_mismatch_non_candidate_framework" for f in soft)
    # Must NOT also be hard-blocked — this is a real SENT job (M4 acceptance bar).
    assert not any(f.severity == "hard" for f in findings)


def test_fairmarkit_clean_despite_linkedin_similar_jobs_contamination() -> None:
    """Real fixture from M4 calibration: the LinkedIn dump's "Similar jobs"
    sidebar contains an unrelated hybrid-Warsaw listing (Synergetica) that used
    to falsely trip the on-site check for Fairmarkit itself (a real, fully
    described Warsaw-office EU role with no hybrid/onsite language of its own)."""
    text = _read("fairmarkit_job_posting.txt")
    findings = assess_job_text(
        text,
        title="Senior Frontend Engineer (Design Systems)",
        company="Fairmarkit",
    )
    assert findings == []


# ── HARD rule (a): foreign on-site/hybrid ───────────────────────────────────


def test_foreign_onsite_hybrid_hard() -> None:
    text = "We need an on-site engineer in our Berlin office 3 days a week."
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "foreign_onsite_hybrid")


def test_foreign_onsite_hybrid_vetoed_by_fully_remote() -> None:
    text = "Fully remote role. Our HQ is in Berlin but you will never need to visit on-site."
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_foreign_onsite_hybrid_vetoed_by_wroclaw_mention() -> None:
    text = "Hybrid role, on-site 2 days/week. Our Wrocław office is a great place to work, near our Berlin partners."
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_foreign_onsite_hybrid_vetoed_by_weekly_hybrid_warsaw() -> None:
    text = (
        "Hybrid role, on-site once a week in our Warsaw office. "
        "The rest of the week is fully remote."
    )
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_foreign_onsite_hybrid_polish_remote_facet_veto() -> None:
    """theprotocol.it's real 'tryb pracy:' facet lists remote as an offered
    mode even when hybrid/stationary are also listed — candidate can choose
    remote. Found during M4 calibration (NASK/B2BNet/IdeoSpZoO false positives)."""
    text = "tryb pracy:\nzdalna •  hybrydowa •  stacjonarna\nRzeszów, podkarpackie\nSome on-site work required."
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_foreign_onsite_hybrid_vetoed_by_perks_bullet() -> None:
    """Real M4 calibration false positive: BitPanda (a real SENT+relocated
    posting, location 'Barcelona, Remote') lists a benefits bullet — 'Fuel and
    focus on-site – Pandas in Vienna, Bucharest, Barcelona, and Berlin can
    enjoy free onsite dining' — that used to trip the HARD rule purely because
    four foreign cities sit within 120 chars of the word 'on-site'/'onsite'."""
    text = (
        "Reward for your impact - a competitive total compensation package. "
        "Fuel and focus on-site - Pandas in Vienna, Bucharest, Barcelona, and Berlin "
        "can enjoy free onsite dining, with freshly prepared lunches and snacks."
    )
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_foreign_onsite_hybrid_not_vetoed_by_unrelated_perks_word_elsewhere() -> None:
    """The perks veto only suppresses the SPECIFIC on-site occurrence it sits
    next to — a genuine foreign on-site requirement elsewhere in the same
    posting must still fire."""
    text = (
        "Free coffee and snacks in our kitchen every day. "
        "This is an on-site role based in our Berlin office, 5 days a week."
    )
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" in _rules(findings)


# ── HARD rule (b): work authorization ───────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Must be a US citizen to apply for this role.",
        "This position requires an active security clearance required for federal work.",
        "Candidates must have H1B sponsorship experience or existing H-1B status.",
        "We only offer W2 employment status; C2C is not accepted.",
        "No visa sponsorship is available for this role.",
    ],
)
def test_work_authorization_hard(text: str) -> None:
    findings = assess_job_text(text)
    assert "unsupported_work_authorization" in _rules(findings)


def test_work_authorization_not_triggered_by_clean_eu_posting() -> None:
    text = "We are looking for a Senior Angular Developer, remote from Poland, B2B contract."
    findings = assess_job_text(text)
    assert "unsupported_work_authorization" not in _rules(findings)


# ── HARD rule (c): unsupported required language ────────────────────────────


@pytest.mark.parametrize(
    "text",
    [
        "Fluent in French is required for this role.",
        "You are a native French speaker with strong communication skills.",
        "Dutch speaking candidates only, C1 Dutch required.",
    ],
)
def test_unsupported_language_hard(text: str) -> None:
    findings = assess_job_text(text)
    assert "unsupported_language_required" in _rules(findings)


def test_unsupported_language_vetoed_by_english_working_language() -> None:
    text = "Our office is in Brussels but English is the working language for all teams, including French colleagues."
    findings = assess_job_text(text)
    assert "unsupported_language_required" not in _rules(findings)


def test_german_still_covered_via_reused_manual_check() -> None:
    text = "German speaking candidates only for this Munich-based team."
    findings = assess_job_text(text)
    assert "is_german_language_required" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "is_german_language_required")


def test_german_vetoed_when_listed_as_nice_to_have() -> None:
    """Real M4 calibration false positive: a real SENT theprotocol.it posting
    (DHCBusinessSolutions) lists 'Nice to have — Optional, 5+ years of
    commercial experience, German language skills, Usage of Nx' — a bonus, not
    a hard requirement."""
    text = (
        "Requirements: 5+ years Angular, TypeScript, RxJS, excellent communication skills.\n"
        "Nice to have: Optional, 5+ years of commercial experience, "
        "German language skills, Usage of Nx.\n"
        "What we offer: Personal development budget."
    )
    findings = assess_job_text(text)
    assert "is_german_language_required" not in _rules(findings)


def test_german_still_hard_when_actually_required_near_nice_to_have_section() -> None:
    """The optional-context veto must not blanket-suppress a genuine requirement
    stated elsewhere in the same posting."""
    text = (
        "Requirements: fluent in German for daily client calls.\n"
        "Nice to have: experience with Nx monorepos."
    )
    findings = assess_job_text(text)
    assert "is_german_language_required" in _rules(findings)


# ── SOFT rule: primary-stack mismatch ───────────────────────────────────────


def test_stack_mismatch_soft_when_only_vue() -> None:
    text = "We use Vue 3, Nuxt and TypeScript across our frontend stack."
    findings = assess_job_text(text)
    assert "stack_mismatch_non_candidate_framework" in _rules(findings)
    assert all(
        f.severity == "soft" for f in findings if f.rule == "stack_mismatch_non_candidate_framework"
    )


@pytest.mark.parametrize(
    "text",
    [
        "We use Vue or Angular depending on the squad.",
        "Experience with React or Vue is required.",
        "Our stack includes Svelte, but React experience is also welcome.",
    ],
)
def test_stack_mismatch_not_triggered_when_candidate_framework_present(text: str) -> None:
    findings = assess_job_text(text)
    assert "stack_mismatch_non_candidate_framework" not in _rules(findings)


def test_stack_mismatch_not_triggered_without_other_framework() -> None:
    text = "We are an Angular shop building a design system used across the company."
    findings = assess_job_text(text)
    assert "stack_mismatch_non_candidate_framework" not in _rules(findings)


# ── SOFT rule: game-engine-first role (2026-07-12 Nexters case) ──────────────


@pytest.mark.parametrize(
    "text",
    [
        # Real 2026-07-12 case: game-engine stack, "Frontend Developer" title.
        "Senior Frontend Developer. Experience with game engines such as "
        "Cocos, Phaser, Babylon, Pixi, and familiarity with the Spine SDK.",
        "We build in TypeScript, C#, or Haxe on PixiJS.",
        "Unity 3D engineer for our casual games studio.",
        "Gameplay engineer working in Unreal Engine and Godot.",
    ],
)
def test_stack_mismatch_game_engine_soft(text: str) -> None:
    findings = assess_job_text(text)
    assert "stack_mismatch_game_engine" in _rules(findings)
    assert all(f.severity == "soft" for f in findings if f.rule == "stack_mismatch_game_engine")
    # Must never HARD-block a game-dev role — it's a warn, not a skip.
    assert not any(f.severity == "hard" for f in findings)


@pytest.mark.parametrize(
    "text",
    [
        "We build browser games in Angular with a Pixi.js rendering layer.",
        "React front-end with a Phaser mini-game embedded on the landing page.",
    ],
)
def test_stack_mismatch_game_engine_not_triggered_when_candidate_framework_present(
    text: str,
) -> None:
    findings = assess_job_text(text)
    assert "stack_mismatch_game_engine" not in _rules(findings)


@pytest.mark.parametrize(
    "text",
    [
        # Bare English words that must NOT be read as game engines.
        "A strong spine of automated tests keeps our releases safe.",
        "The team works in unity toward a shared roadmap.",
    ],
)
def test_stack_mismatch_game_engine_no_english_word_false_positive(text: str) -> None:
    findings = assess_job_text(text)
    assert "stack_mismatch_game_engine" not in _rules(findings)


# ── Reused _MANUAL_SCREEN_CHECKS — HARD tier (no real false positives in M4) ─


def test_ai_training_mill_hard() -> None:
    findings = assess_job_text("Clean Angular Developer role.", company="Micro1 Inc")
    assert "is_ai_training_or_mill" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "is_ai_training_or_mill")


# ── ai_mill_body: mill name in the BODY, company field blank ─────────────────
# The company-based check is blind for Gmail-alert stubs (linkedin.com
# enrichment skipped → company empty) — exactly how QuikHireStaffing/HireFeed
# reached generation on 2026-07-06. The mill's name / apply link is in the
# posting text itself.


def test_mill_name_in_body_hard_even_without_company() -> None:
    text = (
        "Angular Frontend Developer (Remote). Our client is hiring through "
        "micro1's AI-vetted talent network. Apply today!"
    )
    findings = assess_job_text(text)  # no company passed — Gmail-stub scenario
    matches = [f for f in findings if f.rule == "ai_mill_body"]
    assert matches and all(f.severity == "hard" for f in matches)
    assert "micro1" in matches[0].evidence


def test_mill_apply_link_in_body_hard() -> None:
    text = "Frontend Developer - TypeScript (Remote). Apply at https://micro1.com/apply/12345."
    findings = assess_job_text(text)
    assert "ai_mill_body" in _rules(findings)


def test_mill_front_name_in_body_hard() -> None:
    text = "This position is managed by QuikHire Staffing on behalf of our client."
    findings = assess_job_text(text)
    assert "ai_mill_body" in _rules(findings)


def test_mill_body_no_false_positive_on_similar_words() -> None:
    # "micro1" must not fire on "micro-frontends" / "microservices"; "mercor"
    # must not fire inside a longer word.
    text = (
        "Angular role: micro-frontends, microservices, and Micro100 tooling at Mercorp Solutions."
    )
    findings = assess_job_text(text)
    assert "ai_mill_body" not in _rules(findings)


def test_mill_body_respects_exclude_ai_training_flag(monkeypatch) -> None:
    from hunter import filters as filters_mod

    patched = dict(filters_mod.FILTER)
    patched["exclude_ai_training"] = False
    monkeypatch.setattr(filters_mod, "FILTER", patched)
    text = "Hiring through micro1's talent network."
    findings = assess_job_text(text)
    assert "ai_mill_body" not in _rules(findings)


def test_unacceptable_contract_hard() -> None:
    text = "This is a part-time position, 20 hours a week."
    findings = assess_job_text(text)
    assert "is_unacceptable_contract" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "is_unacceptable_contract")


# ── russia_remote_market: Russia-tied role, even a remote one ────────────────
# Owner decision 2026-07-12 (talanto.work links surfaced via the Telegram
# channels source): unclear whether a Russia-based employer can legally/
# practically pay a Poland-based candidate — skip regardless of remote status.


def test_russia_remote_tag_hard() -> None:
    text = "Middle JavaScript Developer\nFull time · Middle · Remote · Russia\ncrm, SQL, JavaScript"
    findings = assess_job_text(text)
    matches = [f for f in findings if f.rule == "russia_remote_market"]
    assert matches and all(f.severity == "hard" for f in matches)
    assert "russia" in matches[0].evidence.lower()


def test_lokatsiya_rf_hard() -> None:
    text = "Middle+ JavaScript разработчик.\nЛокация: РФ.\nФормат: удаленно."
    findings = assess_job_text(text)
    assert "russia_remote_market" in _rules(findings)


def test_tk_rf_outstaff_phrasing_hard() -> None:
    """Real talanto.work (Extyl) case: the tag line only says 'Middle ·
    Remote' with no country, but the body reads 'Оформление в штат компании
    Extyl по ТК РФ' (employment registration per the Russian Labor Code)."""
    text = (
        "Разработчик Angular / Проектная работа / АУТСТАФФ\n"
        "Middle · Remote\n"
        "Оформление в штат компании Extyl по ТК РФ на полную ставку."
    )
    findings = assess_job_text(text)
    assert "russia_remote_market" in _rules(findings)


def test_russia_market_no_false_positive_on_bare_mention() -> None:
    """A bare 'Russia' occurrence that isn't the job's own location TAG must
    not fire — e.g. an employer merely listing Russia among several offices,
    while THIS role reports to the Wrocław team."""
    text = (
        "Senior Frontend Developer (Angular), fully remote within the EU.\n"
        "Our company has offices across Europe, including Poland, Germany, "
        "and Russia, but this role reports to the Wrocław team."
    )
    findings = assess_job_text(text)
    assert "russia_remote_market" not in _rules(findings)


def test_talanto_sidebar_no_longer_false_positives_foreign_onsite() -> None:
    """Real bug found alongside the Russia rule: talanto.work's own sidebar
    ('Hybrid Jobs'/'Office Jobs' sitting near 'USA'/'Canada' within the
    on-site-signal window) tripped foreign_onsite_hybrid on a genuinely
    fully-remote posting. The sidebar is now stripped as a recommendation
    tail before body-level checks run."""
    text = (
        "Senior Frontend Developer (Angular), fully remote worldwide.\n"
        "By Region\nJobs in Europe\nJobs in USA\nJobs in Canada\nJobs in Russia\n"
        "By Format\nRemote Jobs\nRelocation to USA\nHybrid Jobs\nOffice Jobs"
    )
    findings = assess_job_text(text)
    assert "foreign_onsite_hybrid" not in _rules(findings)


def test_unwanted_fullstack_hard() -> None:
    findings = assess_job_text(
        "Backend work in Java and Spring Boot, frontend in Angular.",
        title="Full Stack Developer (Java/Angular)",
    )
    assert "is_unwanted_fullstack" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "is_unwanted_fullstack")


def test_relocation_required_hard() -> None:
    text = "Relocation is required for this role; we do not support remote work."
    findings = assess_job_text(text)
    matches = [f for f in findings if f.rule == "requires_relocation"]
    assert matches and all(f.severity == "hard" for f in matches)


# ── Reused _MANUAL_SCREEN_CHECKS — SOFT tier (downgraded per M4 calibration) ─


def test_body_disqualifier_soft_not_hard() -> None:
    """M4 calibration found real SENT rows with a body_exclude_patterns hit that
    was a minor "nice to have" mention (NASK_2: WordPress buried in a dozen
    other optional tools) or a dual-stack listing (AdvoxStudio: "Magento or
    React/Vue") — too imprecise to silently skip generation, so it warns."""
    text = "Nice to have: WordPress, Selenium, Python, MongoDB, Kafka, Docker, Kubernetes."
    findings = assess_job_text(text)
    matches = [f for f in findings if f.rule == "has_body_disqualifier"]
    assert matches and all(f.severity == "soft" for f in matches)


def test_unwanted_onsite_location_soft_not_hard() -> None:
    """M4 calibration found real SENT rows (Bayer, PeopleVibe, Codest,
    TechRecruitmentAgency) with a genuine Warsaw hybrid/onsite mention the
    owner applied to anyway — downgraded from the initial hard design."""
    text = "This is a hybrid role based in our Warsaw office, 3 days a week on-site."
    findings = assess_job_text(text)
    matches = [f for f in findings if f.rule == "is_unwanted_onsite_location"]
    assert matches and all(f.severity == "soft" for f in matches)


def test_wroclaw_role_not_flagged_by_onsite_check() -> None:
    text = "Hybrid role, 2 days a week on-site in our Wrocław office."
    findings = assess_job_text(text)
    assert "is_unwanted_onsite_location" not in _rules(findings)


# ── screen_job_text() backward-compat contract (paste path, warn-but-allow) ──


def test_screen_job_text_returns_first_finding_evidence() -> None:
    text = "Must be a US citizen to apply. This is a part-time role."
    result = screen_job_text(text)
    assert result is not None
    assert isinstance(result, str)


def test_screen_job_text_none_when_clean() -> None:
    text = "Remote Angular role, B2B contract, no location constraints."
    assert screen_job_text(text) is None


def test_screen_job_text_ignores_unused_location_kw() -> None:
    # `location` kwarg is accepted for backward compatibility but unused.
    assert screen_job_text("Remote Angular role.", location="Warsaw") is None


# ── Recommendation-tail / SEO-footer contamination strip ────────────────────


def test_strip_recommendation_tail_linkedin() -> None:
    text = (
        "Real job content here.\nSimilar jobs\nUnrelated Senior Developer — Other Co, hybrid Warsaw"
    )
    stripped = _strip_recommendation_tail(text)
    assert "Unrelated Senior Developer" not in stripped
    assert "Real job content here." in stripped


def test_strip_recommendation_tail_theprotocol_footer() -> None:
    text = (
        "Real requirements: Angular, TypeScript.\n"
        "Praca w miastach:\nPraca IT Warszawa\n\u2022\nPraca IT Kraków\n"
        "Stanowiska:\nWordpress praca\n\u2022\nBlazor praca"
    )
    stripped = _strip_recommendation_tail(text)
    assert "Wordpress praca" not in stripped
    assert "Real requirements" in stripped


def test_strip_recommendation_tail_pracuj_similar_offers() -> None:
    text = "Real content.\nSprawdź podobne oferty:\nUnrelated Backend Developer — Other Co"
    stripped = _strip_recommendation_tail(text)
    assert "Unrelated Backend Developer" not in stripped


def test_strip_recommendation_tail_noop_when_absent() -> None:
    text = "Clean job posting with no contamination markers at all."
    assert _strip_recommendation_tail(text) == text


def test_has_body_disqualifier_not_triggered_by_stripped_footer() -> None:
    """End-to-end: a theprotocol.it dump whose ONLY 'wordpress' mention is in
    the sitewide SEO footer must not fire has_body_disqualifier at all."""
    text = (
        "Senior Angular Developer, Wrocław, remote.\n"
        "Requirements: Angular, TypeScript, RxJS.\n"
        "Praca w miastach:\nPraca IT Warszawa\n\u2022\nPraca IT Kraków\n"
        "Technologie i narzędzia:\nWordpress praca\n\u2022\nPHP praca"
    )
    findings = assess_job_text(text)
    assert "has_body_disqualifier" not in _rules(findings)


# ── Robustness ───────────────────────────────────────────────────────────────


def test_assess_job_text_empty_input_returns_no_findings() -> None:
    assert assess_job_text("") == []
    assert assess_job_text(None) == []  # type: ignore[arg-type]


def test_assess_job_text_never_raises_on_garbage_input() -> None:
    # Regex-only engine — must not blow up on odd unicode / control chars.
    assess_job_text("\x00\x01 weird \ufeff text \u200b" * 50)


# ── Title-based checks (docs/DOOMED_GATE_PASTE_PLAN.md) ─────────────────────
# Reused from listing-level filters, but now also applied on the manual-paste
# path via a best-effort title guess when no explicit title is known.


def test_title_exclude_pattern_hard_with_explicit_title() -> None:
    """Real calibration case: Santander '.NET Developer (Angular)' - no
    'fullstack' in the title (so _is_unwanted_fullstack never applies), but
    '.NET' alone is exactly what the listing-level filter would have caught."""
    findings = assess_job_text(
        "5+ years Angular, TypeScript, RxJS. Great team.",
        title=".NET Developer (Angular)",
    )
    assert "title_exclude_pattern" in _rules(findings)
    assert all(f.severity == "hard" for f in findings if f.rule == "title_exclude_pattern")


def test_title_exclude_pattern_hard_with_guessed_title() -> None:
    """Same check, but the title is guessed from the raw text (paste path,
    no explicit title known) - first meaningful line of the dump."""
    text = (
        "Skip to main content\n"
        ".NET Developer (Angular)\n"
        "Some Company Warsaw, Poland\n\n"
        "5+ years Angular, TypeScript, RxJS. Great team."
    )
    findings = assess_job_text(text)
    assert "title_exclude_pattern" in _rules(findings)


def test_off_domain_title_soft_with_guessed_title() -> None:
    """Real calibration case: QuantumBlackMcKinsey 'Software Engineer -
    QuantumBlack, AI by McKinsey' - not a frontend title at all."""
    text = (
        "Skip to main content\n"
        "Software Engineer - QuantumBlack, AI by McKinsey\n"
        "McKinsey Warsaw, Poland\n\n"
        "Angular, React and TypeScript experience is a plus for this AI role."
    )
    findings = assess_job_text(text)
    assert "off_domain_title" in _rules(findings)
    assert all(f.severity == "soft" for f in findings if f.rule == "off_domain_title")


def test_off_domain_title_not_triggered_for_frontend_title() -> None:
    text = (
        "Skip to main content\nSenior Angular Developer\nSome Company\n\nAngular, RxJS, TypeScript."
    )
    findings = assess_job_text(text)
    assert "off_domain_title" not in _rules(findings)


def test_title_based_checks_do_not_override_a_known_title() -> None:
    """A guessed title never runs when the real title is already known -
    the guess is purely a paste-path fallback."""
    text = "Skip to main content\n.NET Developer (Angular)\nSome Company\n\nAngular work."
    findings = assess_job_text(text, title="Senior Angular Developer")
    assert "title_exclude_pattern" not in _rules(findings)
    assert "off_domain_title" not in _rules(findings)


def test_guess_title_from_text_skips_boilerplate_lines() -> None:
    from hunter.filters import _guess_title_from_text

    text = "Skip to main content\nSign in\n\nAngular Developer\nComarch Warsaw, Poland"
    assert _guess_title_from_text(text) == "Angular Developer"


def test_guess_title_ignores_chat_intro_line() -> None:
    """Owner report 2026-07-11 (plavno.io paste): a Telegram forward opened with
    conversational Russian — the old first-meaningful-line rule turned it into
    the gate's 'title' and produced a garbage off_domain_title warning quoting
    the chat line as if it were the job title."""
    from hunter.filters import _guess_title_from_text

    text = (
        "Да, тут можно ознакомиться с компанией - plavno.io\n"
        "What you'll build\n"
        "Your role\n"
        "Who we're looking for\n"
    )
    assert _guess_title_from_text(text) == ""


def test_chat_intro_paste_produces_no_off_domain_finding() -> None:
    """End-to-end regression of the plavno.io case: the whole paste (chat intro
    + English posting body with no title-looking line) must yield NO
    off_domain_title finding — no guess means the title checks find nothing."""
    text = (
        "Да, тут можно ознакомиться с компанией - plavno.io\n"
        "We're building an AI-enabled platform for trust managers in private "
        "wealth and looking for a senior generalist to own its frontend.\n"
        "What you'll build\n"
        "Senior Angular/TypeScript experience; deep understanding of reactivity, "
        "state management, and change detection.\n"
    )
    findings = assess_job_text(text)
    assert "off_domain_title" not in _rules(findings)
    assert "title_exclude_pattern" not in _rules(findings)


def test_guess_title_skips_stack_line_that_reads_like_prose() -> None:
    """A body line naming the stack but punctuated like a sentence is prose,
    not a title — it must not become the guessed title."""
    from hunter.filters import _guess_title_from_text

    text = "Senior Angular/TypeScript experience; deep understanding of reactivity.\n"
    assert _guess_title_from_text(text) == ""


def test_guess_title_requires_role_or_stack_signal() -> None:
    """Section headers and slogans without a role noun never qualify."""
    from hunter.filters import _guess_title_from_text

    text = "What you'll build\nYour role\nInterview\nGreat team culture"
    assert _guess_title_from_text(text) == ""


def test_guess_title_scan_is_capped_to_the_top_of_the_text() -> None:
    """A role noun buried deep in the body (past the candidate-line cap) must
    not be mistaken for the title."""
    from hunter.filters import _guess_title_from_text

    filler = "\n".join(f"Some plain line {i}" for i in range(12))
    text = filler + "\nAngular Developer"
    assert _guess_title_from_text(text) == ""


def test_header_location_rule_was_removed_not_just_disabled() -> None:
    """Regression guard: a bare anti-hybrid city mention (no onsite/hybrid
    wording) must never produce a finding - the header-location rule was
    tried and dropped (real Fairmarkit false positive), not just gated off."""
    text = "Angular Developer\nComarch Warsaw, Mazowieckie, Poland\n\nGreat Angular role, remote-friendly team."
    findings = assess_job_text(text, title="Angular Developer", company="Comarch")
    assert "header_location_anti_hybrid_city" not in _rules(findings)
