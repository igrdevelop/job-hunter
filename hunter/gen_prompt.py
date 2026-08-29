"""hunter/gen_prompt.py — assemble the LLM prompts for generation and claim
judging from the tracked, candidate-agnostic prompt files in prompts/ plus a
block of the ACTIVE candidate's own employment facts, rendered at runtime
from candidate.yaml.

Why this exists (docs/GENERATION_ARCHITECTURE_ANALYSIS.md §6, wave 2):
prompts/generation_rules.md and prompts/judge_rules.md used to hardcode the
project owner's own employment history directly in a tracked git file — a
7-employer table with exact periods, per-role backend rules, a university, a
course list. Both pipelines read that file as their ONLY source of
generation rules, so neither a second user nor the owner himself could
change an employment fact without editing a file that ships in the repo.
The facts already live in candidate.yaml (employers.history,
experience.years_label/since_year) — this module renders them into the
prompt text at call time, the same pattern hunter/verdict_refine.py:60-71
already uses for its own (smaller) prompt fragments.

Two pipelines, one prompt: the API pipeline (hunter/apply_api.py) and the
shadow pipeline (hunter/dual_apply.py) import build_generation_prompt()
directly; the claim judge (hunter/claim_judge.py) imports
build_judge_prompt() directly. The CLI skill (.claude/commands/apply.md)
cannot import Python, so it shells out to `python -m hunter.gen_prompt` and
uses stdout verbatim — see __main__ below. Both branches MUST see
byte-identical text for the same candidate.yaml; that is the whole point of
this module existing instead of two hand-maintained copies (exactly the
class of drift that broke the CLI's Polish-CV logic for months).

candidate.yaml is optional (a bare checkout with no employers.history
configured must still produce a WORKING, if unconstrained, prompt rather
than an empty or broken one) — every render function below degrades to a
generic paragraph instead of raising when the relevant candidate.yaml keys
are absent.
"""

from __future__ import annotations

import sys

from hunter import candidate
from hunter.pipeline.folders import CANDIDATE_DIR, PROMPTS_DIR

GENERATION_TEMPLATE_PATH = PROMPTS_DIR / "generation_rules.md"
JUDGE_TEMPLATE_PATH = PROMPTS_DIR / "judge_rules.md"

# Optional per-user narrative tail (story bank, personal cover-letter
# examples) that doesn't fit candidate.yaml's structured fields — see
# candidate/README.md. Gitignored, like candidate_profile.md; absent by
# default, in which case the generation prompt is exactly the tracked file
# plus the rendered employment-facts block, nothing more.
LOCAL_TAIL_FILENAME = "generation_rules.local.md"

_EMPLOYMENT_FACTS_MARKER = "<!-- CANDIDATE_EMPLOYMENT_FACTS -->"
_GROUND_TRUTH_MARKER = "<!-- CANDIDATE_GROUND_TRUTH -->"

# Stack key -> base-CV filename (relative to the candidate dir). Single
# source for both apply_api.py (imports this dict directly) and the CLI
# skill (.claude/commands/apply.md reads it via `base-cv-map` below) —
# previously apply.md hardcoded its own copy of this map and silently
# ignored a user's candidate.yaml tracks.base_cv override (docs/
# GENERATION_ARCHITECTURE_ANALYSIS.md §5.3).
_DEFAULT_BASE_CV_FILES = {
    "angular": "base_cv_angular.md",
    "react": "base_cv_react.md",
    "javascript": "base_cv_react.md",
    "fullstack_angular_nest": "base_cv_fullstack_angular_nest.md",
    "fullstack_react_next": "base_cv_fullstack_react_next.md",
    "ai": "base_cv_ai.md",
}


def base_cv_files() -> dict[str, str]:
    """Stack key -> base-CV filename, with candidate.yaml's tracks.base_cv
    overriding/extending the built-in defaults."""
    return {**_DEFAULT_BASE_CV_FILES, **candidate.get("tracks.base_cv", {})}


def _history() -> list[dict]:
    raw = candidate.get("employers.history", []) or []
    return [r for r in raw if isinstance(r, dict)]


def _years_label() -> str:
    return str(candidate.get("experience.years_label", "") or "").strip()


def _since_year() -> str:
    value = candidate.get("experience.since_year", "")
    return str(value).strip() if value else ""


def render_employment_facts() -> str:
    """The RED-LINE employer table + backend/legacy-stack/track-title rules
    that prompts/generation_rules.md refers to, rendered from candidate.yaml
    (employers.history, experience.years_label/since_year).

    Never raises. Returns a generic fallback paragraph when no history is
    configured — a bare-bones candidate.yaml must still produce a working
    (if unconstrained) prompt.
    """
    history = _history()
    if not history:
        return (
            "## Candidate Employment Facts\n\n"
            "No fixed employment history is configured for this candidate "
            "(candidate.yaml: employers.history is empty). Derive every "
            "experience entry from the candidate profile instead — there is "
            "no fixed company list, order, or per-role backend rule to "
            "enforce beyond what the profile itself states."
        )

    lines = ["## Candidate Employment Facts (from candidate.yaml)", ""]
    lines.append(
        "This is the authoritative, ordered list of the candidate's real "
        "employers. The RED LINES and Step 3 below refer back to it — do "
        "not invent, reorder, merge, or omit any entry."
    )
    lines.append("")
    lines.append("| # | company (exact) | title (exact) | period |")
    lines.append("|---|----------------|---------------|--------|")
    for i, role in enumerate(history, start=1):
        lines.append(
            f"| {i} | {role.get('company', '')} | {role.get('title', '')} | "
            f"{role.get('period', '')} |"
        )
    lines.append("")

    years_label = _years_label()
    if years_label:
        since = _since_year()
        since_note = f" (since {since})" if since else ""
        lines.append(
            f'**Total experience:** always state "{years_label} years"{since_note} '
            "in summaries and cover letters — never round down to a lower number."
        )
        lines.append("")

    backend_roles = [r for r in history if r.get("backend")]
    if backend_roles:
        lines.append("**Backend per role** (never invent a different backend on a `stack_line`):")
        for r in backend_roles:
            lines.append(f"- {r.get('company', '')}: {r.get('backend', '')}")
        lines.append("")

    bullets_roles = [r for r in history if r.get("bullets_max")]
    if bullets_roles:
        lines.append("**Bullet budget for compression** (fit 2 pages without dropping a role):")
        for r in bullets_roles:
            lines.append(f"- {r.get('company', '')}: {r.get('bullets_max')} bullets")
        lines.append("")

    legacy = [r.get("company", "") for r in history if r.get("legacy_stack_ok")]
    modern = [r.get("company", "") for r in history if not r.get("legacy_stack_ok")]
    if legacy:
        lines.append(
            "**Era-appropriate stack (jQuery and other pre-2022 tech):** only "
            f"realistic on {', '.join(legacy)}. NEVER add legacy-era tech to "
            f"{', '.join(modern) if modern else 'any other role'} — those are "
            "modern roles that do not use it."
        )
        lines.append("")

    track_overrides = [r for r in history if r.get("title_by_track")]
    if track_overrides:
        lines.append(
            "**Track-specific title overrides** (the ONLY variation to a "
            "role's `title` besides the Angular/React framework swap — see "
            "the RED LINE above):"
        )
        for r in track_overrides:
            company = r.get("company", "")
            base_title = r.get("title", "")
            for track, title in (r.get("title_by_track") or {}).items():
                lines.append(
                    f'- {company}: on the `{track}` track, use "{title}" instead of "{base_title}"'
                )
        lines.append("")

    flexible_name = candidate.get("employers.flexible.name", "")
    flexible_projects = candidate.get("employers.flexible.projects", []) or []
    if flexible_name and flexible_projects:
        lines.append(
            f"**Flexible-employer sub-projects:** {flexible_name} covered several "
            f"client projects ({', '.join(str(p) for p in flexible_projects)}) — a "
            "sub-project's `stack_line` may show React OR Angular depending on "
            "track, never both in the same one."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_ground_truth() -> str:
    """The judge's own candidate-specific ground truth (real employers,
    years of experience), rendered from candidate.yaml. Never raises."""
    protected = [str(x) for x in (candidate.get("employers.protected", []) or [])]
    flexible = str(candidate.get("employers.flexible.name", "") or "").strip()
    names = protected + ([flexible] if flexible and flexible not in protected else [])
    years_label = _years_label()

    if not names and not years_label:
        return (
            "## Candidate Ground Truth\n\n"
            "No fixed employer list is configured for this candidate "
            "(candidate.yaml: employers.protected is empty) — treat every "
            "employer, client, and number in the candidate profile as "
            "ground truth instead."
        )

    lines = ["## Candidate Ground Truth (from candidate.yaml)", ""]
    if names:
        lines.append(
            "The candidate's REAL, verifiable employers/clients are: "
            + ", ".join(names)
            + ". Any client name, prestige claim, or scale beyond what the "
            "candidate profile documents for these is fabricated."
        )
        lines.append("")
    if years_label:
        lines.append(
            f"The candidate has {years_label} years of experience — this, and "
            "any other numeric claim actually stated in the profile (team/org "
            "sizes, counts, percentages), is real and must be preserved, never "
            "flagged."
        )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _local_tail(cand_dir=None) -> str:
    """Optional per-user narrative appended after the tracked prompt (story
    bank, personal cover-letter examples) — see candidate/README.md. Returns
    "" when absent; never raises."""
    directory = cand_dir if cand_dir is not None else CANDIDATE_DIR
    path = directory / LOCAL_TAIL_FILENAME
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8").strip()
    return f"\n\n---\n\n{text}\n" if text else ""


def build_generation_prompt(*, cand_dir=None) -> str:
    """The full generation system prompt: the tracked, candidate-agnostic
    prompts/generation_rules.md with its <!-- CANDIDATE_EMPLOYMENT_FACTS -->
    marker replaced by the rendered employment-facts block, plus the
    optional local narrative tail."""
    template = GENERATION_TEMPLATE_PATH.read_text(encoding="utf-8")
    rendered = template.replace(_EMPLOYMENT_FACTS_MARKER, render_employment_facts().rstrip())
    return rendered + _local_tail(cand_dir)


def build_judge_prompt() -> str:
    """The full claim-judge system prompt: the tracked prompts/judge_rules.md
    with its <!-- CANDIDATE_GROUND_TRUTH --> marker replaced by the rendered
    ground-truth block."""
    template = JUDGE_TEMPLATE_PATH.read_text(encoding="utf-8")
    return template.replace(_GROUND_TRUTH_MARKER, render_ground_truth().rstrip())


def _print_base_cv_map() -> None:
    for key, filename in base_cv_files().items():
        print(f"{key}={filename}")


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    subcommand = args[0] if args else "generation"

    if subcommand == "generation":
        sys.stdout.write(build_generation_prompt())
    elif subcommand == "judge":
        sys.stdout.write(build_judge_prompt())
    elif subcommand == "base-cv-map":
        _print_base_cv_map()
    else:
        print(f"unknown subcommand: {subcommand!r} (expected generation|judge|base-cv-map)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
