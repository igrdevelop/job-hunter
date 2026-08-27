"""Safety net for docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1: as
hunter/apply_shared.py is decomposed into hunter/pipeline/*, this file pins
every name currently imported from ``hunter.apply_shared`` anywhere in the
repo (tests, apply_api.py, apply_cli.py, dual_apply.py, verdict_refine.py,
claim_judge.py, repost_gate.py, resume_sanitizer.py, tools/preview_judge.py),
including underscored/private names and attributes only ever accessed via
qualified module access (``apply_shared.requests.post``, ``apply_shared.
APPLICATIONS_DIR``). It must stay green, unmodified, through every step of
the migration — a name disappearing from ``hunter.apply_shared`` here is a
backward-compat break, not a refactor.

Written BEFORE any code moved out of apply_shared.py (per the migration
plan), from a grep of every ``from hunter.apply_shared import ...`` /
``from hunter import apply_shared`` + ``apply_shared.<attr>`` / monkeypatch
site in the repo at the start of the migration.
"""

from __future__ import annotations

import importlib

import pytest

# Every name that must remain importable as `hunter.apply_shared.<NAME>`,
# whether via `from hunter.apply_shared import NAME` or qualified attribute
# access (`apply_shared.NAME`). Order matches roughly the original file.
EXPECTED_NAMES = [
    # Exit codes / placeholders (hunter/pipeline/errors.py)
    "APPLY_MANUAL_EXIT_CODE",
    "APPLY_RATE_LIMITED_EXIT_CODE",
    "APPLY_LLM_OUTAGE_EXIT_CODE",
    "PASTE_NO_URL_PLACEHOLDER",
    "ApplyError",
    "is_rate_limit_error",
    "is_transient_fetch_error",
    # LLM profile helpers (hunter/pipeline/profiles.py)
    "_llm_p",
    "_translate_p",
    # Telegram (hunter/pipeline/notify.py) + the config constants notify()
    # reads dynamically from this module (tests/conftest.py's autouse
    # _no_telegram fixture patches these two directly on hunter.apply_shared)
    "notify",
    "send_telegram_documents",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "requests",  # qualified access: apply_shared.requests.post
    # Output folder (hunter/pipeline/folders.py)
    "PROMPTS_DIR",
    "CANDIDATE_DIR",
    "compute_output_folder",
    "_sanitize_folder_company",
    "APPLICATIONS_DIR",  # config re-export; monkeypatched via apply_shared
    # Content validation (hunter/pipeline/validate.py)
    "REQUIRED_JSON_KEYS",
    "validate_content",
    # Pre-LLM stack screening + doomed/prescreen gates (hunter/pipeline/gates.py)
    "is_react_only_job_text",
    "is_backend_only_job_text",
    "_already_processed",
    "run_doomed_gate",
    "stack_gate_allows_manual",
    "run_prescreen",
    "_REACT_SKIP_FORCE_HINT",
    # Post-generation abort (hunter/pipeline/abort.py)
    "abort_after_generation",
    "_handle_jobleads_fetch_blocked",
    # Language enforce-gate (hunter/pipeline/lang.py)
    "enforce_language_separation",
    "ensure_pl_resume",
    "build_pl_skip_instruction",
    "_translate_resume",
    "_translate_plain",
    # ATS keyword loop (hunter/pipeline/ats.py)
    "build_ats_keyword_checklist",
    "_ats_check_loop",
    "_filter_self_description_keywords",
    # Content scrubs (hunter/pipeline/scrubs.py)
    "_scrub_compliance_clause",
    "_strip_compliance_claims",
    "_prestige_claim_re",
    "_scrub_prestige_text",
    "_strip_prestige_claims",
    "_split_skill_items",
    "_collapse_gloss_item",
    "_dedup_skill_glosses",
]


@pytest.mark.parametrize("name", EXPECTED_NAMES)
def test_name_is_importable_from_apply_shared(name: str) -> None:
    """`from hunter.apply_shared import <name>` must keep working."""
    mod = importlib.import_module("hunter.apply_shared")
    assert hasattr(mod, name), f"hunter.apply_shared.{name} is missing"


def test_from_import_star_style_still_resolves_each_name() -> None:
    """Belt-and-braces: exercise the actual `from hunter.apply_shared import
    X` statement (not just getattr) for a representative sample of names,
    the exact style used by apply_api.py / apply_cli.py / dual_apply.py."""
    from hunter.apply_shared import (  # noqa: F401
        ApplyError,
        APPLY_LLM_OUTAGE_EXIT_CODE,
        APPLY_MANUAL_EXIT_CODE,
        APPLY_RATE_LIMITED_EXIT_CODE,
        PASTE_NO_URL_PLACEHOLDER,
        REQUIRED_JSON_KEYS,
        CANDIDATE_DIR,
        PROMPTS_DIR,
        _already_processed,
        _ats_check_loop,
        _collapse_gloss_item,
        _dedup_skill_glosses,
        _filter_self_description_keywords,
        _handle_jobleads_fetch_blocked,
        _llm_p,
        _prestige_claim_re,
        _sanitize_folder_company,
        _scrub_compliance_clause,
        _scrub_prestige_text,
        _split_skill_items,
        _strip_compliance_claims,
        _strip_prestige_claims,
        _translate_resume,
        abort_after_generation,
        build_ats_keyword_checklist,
        build_pl_skip_instruction,
        compute_output_folder,
        enforce_language_separation,
        ensure_pl_resume,
        is_backend_only_job_text,
        is_rate_limit_error,
        is_react_only_job_text,
        is_transient_fetch_error,
        notify,
        run_doomed_gate,
        run_prescreen,
        send_telegram_documents,
        stack_gate_allows_manual,
        validate_content,
    )


def test_qualified_module_attribute_access_still_works() -> None:
    """A handful of tests patch these via `hunter.apply_shared.<attr>`
    (monkeypatch.setattr with a string path) rather than importing the name —
    that requires the attribute to exist on the *module object*, which a
    `from X import Y`-only re-export also satisfies, but `apply_shared.
    requests.post` additionally requires `requests` itself to be an
    attribute of hunter.apply_shared (not just used internally by whichever
    submodule now owns notify())."""
    from hunter import apply_shared

    assert hasattr(apply_shared, "requests")
    assert hasattr(apply_shared.requests, "post")
    assert hasattr(apply_shared, "TELEGRAM_BOT_TOKEN")
    assert hasattr(apply_shared, "TELEGRAM_CHAT_ID")
    assert hasattr(apply_shared, "APPLICATIONS_DIR")
    assert hasattr(apply_shared, "notify")
    assert callable(apply_shared.notify)
