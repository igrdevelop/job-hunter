"""Candidate identity and employment history.

Copy this file:  cp candidate_config.example.py candidate_config.py
Then edit candidate_config.py with YOUR real data.
"""

# ── CV header ───────────────────────────────────────────────────────
CANDIDATE_NAME = "Jane Doe"
CANDIDATE_SUBTITLE = ""  # e.g. "also known as Igor Pietraszewski" — leave "" if none
CANDIDATE_HEADLINE = "Senior Frontend Developer ({stack})"  # {stack} is replaced at generation time
CANDIDATE_CONTACT = (
    "+48 000 000 000 | jane.doe@example.com | linkedin.com/in/janedoe | Warsaw, Poland"
)

# ── CV filename ─────────────────────────────────────────────────────
CV_FILENAME_PREFIX = "Jane_Doe"  # -> Jane_Doe_CV_Angular_2026_EN.pdf

# ── Employment history (for pipeline validation + safety) ───────────
EXPECTED_ROLE_COUNT = 4  # how many roles in your candidate_profile.md

# Lowercase company names used for loose substring matching (content_qa.py
# checks a generated resume's company field against this set).
REAL_COMPANIES = {"acme corp", "beta solutions", "gamma studio", "delta inc"}

# Same companies, properly cased and in candidate_profile.md order — used only
# to render a human-readable "these are ALL your roles, in this order" hint in
# the LLM repair prompt (hunter/apply_api.py). Keep this in sync with
# REAL_COMPANIES / EXPECTED_ROLE_COUNT above.
REAL_COMPANIES_ORDERED = ("Acme Corp", "Beta Solutions", "Gamma Studio", "Delta Inc")

PROFILE_TITLES = {  # lowercase canonical titles from candidate_profile.md
    "senior frontend developer (angular)",
    "frontend developer",
    "junior frontend developer",
    "intern",
}

# Employers the verdict-refine stretch loop must NEVER invent experience for
PROTECTED_EMPLOYERS = ("Acme Corp", "Beta Solutions", "Gamma Studio")

# Flexible past projects a stretch-round tech MAY be woven into
# (set empty tuple if none)
FLEXIBLE_PROJECTS = ()

# Employer names to allowlist in lang_guard.py (won't be flagged as Polish words)
EMPLOYER_TECH_TERMS = {"acme", "beta", "gamma", "delta"}
