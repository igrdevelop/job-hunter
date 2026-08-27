"""
hunter/apply_shared.py — backward-compat re-export shim for hunter/pipeline/*.

Everything that used to live here (shared helpers for apply_api / apply_cli /
dual_apply / verdict_refine / claim_judge / repost_gate) moved to
hunter/pipeline/ (docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1). This
module re-exports it all, including underscored/private names, so the ~32
existing import sites across the repo keep working unchanged. New code
should import directly from the specific hunter.pipeline.* submodule.

See tests/test_apply_shared_shim.py for the pinned list of names this module
must keep exposing.
"""

from __future__ import annotations

import requests  # noqa: F401 — kept for `apply_shared.requests.post` backward compat

# Kept as DIRECT (not re-exported) imports: several hunter.pipeline.* functions
# read these back from hunter.apply_shared dynamically at call time (see their
# own docstrings for why) — this module must stay their live source of truth.
# It's also the attribute path several tests monkeypatch
# (tests/conftest.py's autouse `_no_telegram` fixture, tests/test_apply_shared.py,
# tests/test_repost_gate.py, tests/test_cli_empty_run.py, the golden E2E tests).
from hunter.config import (  # noqa: F401
    APPLICATIONS_DIR,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    TELEGRAM_SEND_DOCS,
)

# ── Re-exports (docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) ────────────
# These symbols now live in hunter/pipeline/*; re-exported here for backward
# compat (32 call sites across the repo import them from hunter.apply_shared).
from hunter.pipeline.abort import (  # noqa: F401
    _handle_jobleads_fetch_blocked,
    _write_abort_skip_row,
    abort_after_generation,
)
from hunter.pipeline.ats import (  # noqa: F401
    _ats_check_loop,
    _filter_self_description_keywords,
    build_ats_keyword_checklist,
)
from hunter.pipeline.errors import (  # noqa: F401
    APPLY_LLM_OUTAGE_EXIT_CODE,
    APPLY_MANUAL_EXIT_CODE,
    APPLY_RATE_LIMITED_EXIT_CODE,
    PASTE_NO_URL_PLACEHOLDER,
    ApplyError,
    is_rate_limit_error,
    is_transient_fetch_error,
)
from hunter.pipeline.folders import (  # noqa: F401
    CANDIDATE_DIR,
    PROMPTS_DIR,
    _sanitize_folder_company,
    compute_output_folder,
)
from hunter.pipeline.gates import (  # noqa: F401
    _already_processed,
    _REACT_SKIP_FORCE_HINT,
    is_backend_only_job_text,
    is_react_only_job_text,
    run_doomed_gate,
    run_prescreen,
    stack_gate_allows_manual,
)
from hunter.pipeline.lang import (  # noqa: F401
    build_pl_skip_instruction,
    enforce_language_separation,
    ensure_pl_resume,
    _translate_plain,
    _translate_resume,
)
from hunter.pipeline.notify import notify, send_telegram_documents  # noqa: F401
from hunter.pipeline.profiles import _llm_p, _translate_p  # noqa: F401
from hunter.pipeline.scrubs import (  # noqa: F401
    _collapse_gloss_item,
    _dedup_skill_glosses,
    _prestige_claim_re,
    _scrub_compliance_clause,
    _scrub_prestige_text,
    _split_skill_items,
    _strip_compliance_claims,
    _strip_prestige_claims,
)
from hunter.pipeline.validate import REQUIRED_JSON_KEYS, validate_content  # noqa: F401
