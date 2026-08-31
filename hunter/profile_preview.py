"""hunter/profile_preview.py — deterministic, $0, no-LLM "test resume" preview
for a structured resume Profile (hunter/profile_schema.py).

docs/PROFILE_PAGE_TABS_WORKORDER.md, Tab 4 (Test resume) + the "Bot-repo work
item": the site's preview tab needs to show "what will the system actually
produce from my profile" without spending an LLM call — there is no real
vacancy to generate against. This module assembles a generic,
content.json-shaped dict straight from the Profile document (no fabricated
claims, nothing an LLM would normally supply) and drives the SAME
generate_docs.py machinery the real apply pipeline uses to render it, so the
preview PDF is genuinely the production layout, not a look-alike.

Track resolution mirrors hunter.profile_render exactly (bullets_by_track
wholesale override, Role.tracks visibility, variant skills override) for a
real track key. `track == "core"` is a special case, deliberately UNFILTERED:
Core is the facts view (same philosophy as render_profile_md() and the
employers.history projection, which also ignore Role.tracks/Bullet.tracks —
see profile_render.py's module docstring), not one track's presentation, so
a core preview shows every role/bullet/skill regardless of tags.

No LLM call anywhere, and none of the pipeline machinery that assumes one
(claim judge, ATS verdict, verdict-refine, doomed gate, ...) runs here —
`render_preview()` calls generate_docs.py directly, the same script the real
pipeline calls at its very last step, with `--no-tracker` (never a tracker
row — a preview is not an application) and `GENERATE_ABOUT_ME_PL=false`
(generate_docs.py's about-me step is an LLM call and defaults to ON even in
short/non-full mode; the preview must stay LLM-free)."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

from hunter.profile_render import _role_bullets_for_track, _variant_skills, _visible_roles_for_track
from hunter.profile_schema import Core, Profile, Role, SkillCategory

CORE_TRACK = "core"

# Same slug shape hunter/schedules/profile_jobs.py already requires of a
# track ('core' plus the known track keys: angular/react/ai/fullstack_*) —
# a track string crosses a process boundary via a shared DB row (the API
# writes the payload) and must never be trusted as a path component before
# validation. 'core' itself already matches this pattern.
_TRACK_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Mirrors generate_docs.py's own _DEFAULT_SKILL_CATEGORIES / the
# gen_profile.py builtin "document.skill_categories" default. The real apply
# pipeline's generation prompt tells the LLM to bucket the candidate's skills
# into exactly these four sections; a deterministic preview has no model to
# do that re-bucketing, so it matches each configured category's LABEL
# case-insensitively against the profile's own SkillCategory.category names.
# A profile category whose label doesn't match one of these four (e.g. a
# freeform "AI Dev Tools") is simply not shown in this fixed-shape CV
# section — an honest limitation shared with the real pipeline for any
# category a user's own generation.yaml doesn't also configure.
_DEFAULT_SKILL_CATEGORIES = [
    {"key": "frontend", "label": "Frontend"},
    {"key": "tools", "label": "Tools"},
    {"key": "methodologies", "label": "Methodologies"},
    {"key": "languages", "label": "Languages"},
]

# Wall-clock cap for the generate_docs.py subprocess: sanitize + DOCX build +
# one LibreOffice PDF conversion (convert_all_to_pdf's own internal timeout
# is 120s) — no LLM call is on this path, so this is generous headroom, not
# a budget for a slow model response.
PREVIEW_TIMEOUT_SEC = 180


class PreviewError(RuntimeError):
    """Raised when a preview cannot be built or rendered."""


def validate_track(track: str) -> str:
    """Validate a track string before it is used as a path component or a
    profile.variants lookup key. Raises PreviewError on anything that is not
    a simple lowercase slug ('core' or a known track key shape) — the
    payload originates from another process (the API) over a shared DB row,
    or from a CLI caller, and must not be trusted at face value."""
    cleaned = (track or "").strip()
    if not _TRACK_RE.match(cleaned):
        raise PreviewError(f"invalid or unsafe track: {track!r}")
    return cleaned


def _stack_label(track: str) -> str:
    return "General" if track == CORE_TRACK else track.capitalize()


def _skills_for_track(profile: Profile, track: str) -> list[SkillCategory]:
    if track == CORE_TRACK:
        return list(profile.core.skills)
    return _variant_skills(profile, track)


def _skills_dict(profile: Profile, track: str) -> dict[str, str]:
    categories = _skills_for_track(profile, track)
    by_label = {c.category.strip().lower(): c for c in categories if c.category.strip()}
    out: dict[str, str] = {}
    for entry in _DEFAULT_SKILL_CATEGORIES:
        match = by_label.get(entry["label"].strip().lower())
        if match and match.items:
            out[entry["key"]] = ", ".join(match.items)
    return out


def _visible_roles(core: Core, track: str) -> list[Role]:
    if track == CORE_TRACK:
        return list(core.roles)
    return _visible_roles_for_track(core, track)


def _role_view(role: Role, track: str) -> tuple[str, str, str, list[str]]:
    """Resolve (title, subtitle, stack_line, bullets) for `role` as rendered
    on `track` — 'core' returns the raw unfiltered fields (the facts), any
    other track applies the same per-track overrides render_base_cv uses."""
    if track == CORE_TRACK:
        bullets = [b.text for b in role.bullets if b.text.strip()]
        return role.title, role.subtitle, role.stack_line, bullets
    title = role.title_by_track.get(track, role.title)
    subtitle = role.subtitle_by_track.get(track, role.subtitle)
    stack_line = role.stack_line_by_track.get(track, role.stack_line)
    bullets = _role_bullets_for_track(role, track)
    return title, subtitle, stack_line, bullets


def _experience_entries(core: Core, track: str) -> list[dict]:
    entries: list[dict] = []
    for role in _visible_roles(core, track):
        title, subtitle, stack_line, bullets = _role_view(role, track)
        entries.append(
            {
                "title": title,
                "company": role.company,
                "period": role.period,
                "subtitle": subtitle,
                "bullets": bullets,
                "stack_line": stack_line,
            }
        )
    return entries


def build_preview_content(profile: Profile, track: str) -> dict:
    """Assemble a generic, no-vacancy content.json-shaped dict deterministically
    from `profile`, resolved for `track`. Does NOT set `output_folder` — the
    caller (a specific output directory per run) fills that in. Every field
    only an LLM would normally produce for a real vacancy (cover letters,
    about_me, ats self-score) is simply omitted; generate_docs.py already
    treats all of those as optional."""
    core = profile.core
    variant = None if track == CORE_TRACK else profile.variants.get(track)
    summary = ((variant.summary if variant else "") or core.summary).strip()
    education = "; ".join(e.text.strip() for e in core.education.entries if e.text.strip())

    return {
        "stack": _stack_label(track),
        "resume_en": {
            "summary": summary,
            "skills": _skills_dict(profile, track),
            "experience": _experience_entries(core, track),
            "education": education,
            "courses": "",
        },
    }


def render_preview(
    profile: Profile,
    track: str,
    out_dir: Path,
    *,
    extra_env: dict[str, str] | None = None,
    python_executable: str | None = None,
    timeout_sec: int = PREVIEW_TIMEOUT_SEC,
) -> list[Path]:
    """Build the preview content and render it via generate_docs.py into
    `out_dir`. Returns the written document paths (PDF first, `content.json`
    itself excluded — it stays on disk for diagnostics, same convention as
    `hunter.apply_shared.abort_after_generation`'s job_posting.txt/
    content.json). Never a tracker row (`--no-tracker`), never an LLM call
    (`GENERATE_ABOUT_ME_PL=false` overrides generate_docs.py's own default-on
    about-me step).

    `extra_env` overlays the subprocess environment — a bot caller passes
    `hunter.users.user_env(user_id)` so identity/candidate.yaml resolve for
    the right user; a CLI caller (tools/preview_profile.py) omits it and
    inherits whatever CANDIDATE_YAML_PATH the ambient process already has.

    Raises PreviewError if the underlying generate_docs.py run fails (e.g.
    the resolved candidate.yaml has no configured identity — see
    hunter.candidate.require_identity) — nothing is left half-rendered
    beyond content.json itself, which generate_docs.py's identity gate
    checks before writing any document.
    """
    track = validate_track(track)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    content = build_preview_content(profile, track)
    content["output_folder"] = str(out_dir)
    content_json_path = out_dir / "content.json"
    content_json_path.write_text(
        json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    from hunter.config import PROJECT_DIR

    generate_docs_script = Path(PROJECT_DIR) / "generate_docs.py"
    python_exe = python_executable or sys.executable
    env = dict(os.environ)
    if extra_env:
        env.update(extra_env)
    env["GENERATE_ABOUT_ME_PL"] = "false"

    try:
        result = subprocess.run(
            [python_exe, str(generate_docs_script), str(content_json_path), "--no-tracker"],
            capture_output=True,
            text=True,
            env=env,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired as e:
        raise PreviewError(f"generate_docs.py timed out after {timeout_sec}s") from e

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise PreviewError(f"generate_docs.py failed (exit {result.returncode}): {detail}")

    pdfs = sorted(out_dir.glob("*.pdf"))
    others = sorted(
        p
        for p in out_dir.iterdir()
        if p.is_file() and p.suffix.lower() != ".pdf" and p != content_json_path
    )
    return [*pdfs, *others]
