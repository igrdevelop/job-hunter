"""hunter/profile_render.py — deterministic render of a structured resume
Profile (hunter/profile_schema.py) into the three files the apply pipeline
actually reads: candidate.yaml, candidate_profile.md, base_cv_<track>.md
(docs/RESUME_PROFILE_STORE_PLAN.md M2).

The pipeline (apply_api.py, apply_cli.py, generate_docs.py, hunter/candidate.py,
hunter/filters.py, hunter/gen_prompt.py) is NOT touched — it keeps reading
those three files at the same paths. This module's only job is to produce
byte-identical-in-spirit versions of them from the structured store, so a
rendered file is indistinguishable from a hand-written one to every consumer.

Derived fields (M0b): `employers.real_companies`, `employers.profile_titles`
and `employers.history` are NOT stored on the Profile — they are computed
here from `core.employers.protected`/`flexible` and `core.roles`, so there is
exactly one place to edit an employer name instead of three hand-synced
copies. `employers.history` must match the exact shape
`hunter/gen_prompt.py::render_employment_facts()` reads (company/title/period
+ optional backend/bullets_max/legacy_stack_ok/title_by_track) — see
render_candidate_yaml()'s docstring.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from pathlib import Path

import yaml

from hunter.profile_schema import Bullet, Core, Profile, Role, SkillCategory

# Polish letters that do NOT decompose under NFKD (ł most famously), plus the
# ones that do — kept explicit so the slug for a Polish city is predictable.
# The source_urls slugs go verbatim into pracuj/theprotocol/jobleads listing
# URLs, which are ASCII ("wroclaw", "warszawa") — "wrocław" would silently
# break all three sources' listing fetches.
_PL_TRANSLIT = str.maketrans(
    "ąćęłńóśźżĄĆĘŁŃÓŚŹŻ",
    "acelnoszzACELNOSZZ",
)


def _ascii_city_slug(city: str) -> str:
    text = city.strip().lower().translate(_PL_TRANSLIT)
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if c.isascii() and not unicodedata.combining(c))


def _dedup_preserve_order(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _employers_protected(core: Core) -> list[str]:
    """Explicit `core.employers.protected` wins; otherwise every role's
    company except the flexible employer (M0b: "defaults to all role
    companies except flexible.name")."""
    if core.employers.protected:
        return list(core.employers.protected)
    flexible_name = core.employers.flexible.name
    return _dedup_preserve_order(
        role.company for role in core.roles if role.company and role.company != flexible_name
    )


def _real_companies(core: Core, protected: list[str]) -> list[str]:
    """Every name the content-QA "is this a real employer" check must accept:
    protected + flexible + EVERY role's company. Protected is not a superset
    of role companies on real data — the owner's protected list holds end
    CLIENTS (the names a CV is verified against), while the actual employers
    (agencies/bodyshops) appear only as role companies; deriving from
    protected alone made content_qa flag every agency employer as unknown."""
    names = list(protected)
    if core.employers.flexible.name:
        names.append(core.employers.flexible.name)
    names.extend(role.company for role in core.roles)
    return _dedup_preserve_order(name.strip().lower() for name in names)


def _profile_titles(core: Core) -> list[str]:
    """Canonical role titles incl. every per-track override — the react
    track's "Senior Frontend Developer (React)" exists only in
    title_by_track, and content_qa must accept it as a known title."""
    titles = [role.title for role in core.roles]
    for role in core.roles:
        titles.extend(role.title_by_track.values())
    return _dedup_preserve_order(title.strip().lower() for title in titles)


def _history_entry(role: Role) -> dict:
    """Project one Role down to a wave-2 `employers.history` entry — exactly
    the fields hunter/gen_prompt.py::render_employment_facts() reads, in the
    same key order candidate.yaml.example uses. Deliberately excludes
    `description`/`bullets` (narrative, not a prompt fact) and
    subtitle_by_track/stack_line_by_track/bullets_by_track (per-track
    rendering deltas, not part of the wave-2 history contract)."""
    entry: dict = {
        "company": role.company,
        "title": role.title,
        "period": role.period,
    }
    if role.backend:
        entry["backend"] = role.backend
    if role.bullets_max:
        entry["bullets_max"] = role.bullets_max
    if role.legacy_stack_ok:
        entry["legacy_stack_ok"] = True
    if role.title_by_track:
        entry["title_by_track"] = dict(role.title_by_track)
    return entry


def render_candidate_yaml(profile: Profile) -> str:
    """Render `candidate.yaml`: every M0a dotpath, with employers.real_companies
    / employers.profile_titles / employers.history / tracks.base_cv computed
    from the profile rather than stored on it (see module docstring)."""
    core = profile.core
    protected = _employers_protected(core)

    data: dict = {
        "identity": {
            "full_name": core.identity.full_name,
            "aka": core.identity.aka,
            "cv_filename_prefix": core.identity.cv_filename_prefix,
            "headline": core.identity.headline,
            "contact": core.identity.contact,
        },
        "location": {
            "home_city": core.location.home_city,
            "home_city_aliases": list(core.location.home_city_aliases),
            "acceptable_hybrid": list(core.location.acceptable_hybrid),
            "weekly_hybrid": list(core.location.weekly_hybrid),
            "work_authorization": core.location.work_authorization,
        },
        "languages": {
            "spoken": list(core.languages.spoken),
            "cv_languages": list(core.languages.cv_languages),
            "disqualify_required": list(core.languages.disqualify_required),
        },
        "employers": {
            "protected": protected,
            "flexible": {
                "name": core.employers.flexible.name,
                "period": core.employers.flexible.period,
                "projects": list(core.employers.flexible.projects),
            },
            "real_companies": _real_companies(core, protected),
            "profile_titles": _profile_titles(core),
            "history": [_history_entry(role) for role in core.roles],
        },
        "education": {
            "school_keyword": core.education.school_keyword,
            "expected_role_count": core.education.expected_role_count,
        },
        "experience": {
            "years_label": core.experience.years_label,
            "since_year": core.experience.since_year,
        },
        "tracks": {
            "base_cv": {track: f"base_cv_{track}.md" for track in profile.variants},
        },
        "source_urls": {
            "pracuj_location": _ascii_city_slug(core.location.home_city),
            "theprotocol_location": _ascii_city_slug(core.location.home_city),
            "jobleads_location": _ascii_city_slug(core.location.home_city),
        },
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, default_flow_style=False)


def _role_bullets(role: Role) -> list[str]:
    """The narrative superset for candidate_profile.md: ALL bullets, never
    filtered by track (that's base_cv's job — see render_base_cv, step 2c)."""
    return [bullet.text for bullet in role.bullets if bullet.text.strip()]


def _render_role_md(role: Role) -> list[str]:
    lines: list[str] = [f"**{role.title} | {role.company}** - {role.period}"]
    # May be "" — this line's mere presence (not its content) is what keeps
    # candidate_profile.md parseable by hunter/resume_sanitizer.py's role
    # regex, which expects exactly one line between the header and the body.
    lines.append(role.subtitle)
    if role.description.strip():
        lines.append("")
        lines.append(role.description.strip())
    bullets = _role_bullets(role)
    if bullets:
        lines.append("")
        lines.extend(f"- {text}" for text in bullets)
    if role.stack_line.strip():
        lines.append(f"Stack: {role.stack_line.strip()}.")
    return lines


def render_profile_md(profile: Profile) -> str:
    """Render `candidate_profile.md`: the free-text career narrative fed to
    the generation LLM. Pure concatenation of whatever the profile holds —
    no text is invented, and an empty section is skipped silently."""
    core = profile.core
    lines: list[str] = ["## Candidate Profile", ""]

    header_lines: list[str] = []
    if core.identity.full_name.strip():
        header_lines.append(f"**Name**: {core.identity.full_name}")
    if core.identity.headline.strip():
        header_lines.append(f"**Headline**: {core.identity.headline}")
    if core.identity.contact.strip():
        header_lines.append(f"**Contact**: {core.identity.contact}")
    if header_lines:
        lines.extend(header_lines)
        lines.append("")

    if core.summary.strip():
        lines.append(core.summary.strip())
        lines.append("")

    if core.roles:
        lines.append("---")
        lines.append("")
        lines.append("### Work Experience")
        lines.append("")
        for i, role in enumerate(core.roles):
            if i:
                lines.append("")
            lines.extend(_render_role_md(role))
        lines.append("")

    education_text = "; ".join(e.text for e in core.education.entries if e.text.strip())
    if education_text:
        lines.append("---")
        lines.append("")
        lines.append(f"**Education**: {education_text}")
        lines.append("")

    if core.languages.spoken:
        lines.append(f"**Languages**: {', '.join(core.languages.spoken)}")
        lines.append("")

    extras_text = [extra.text for extra in core.extras if extra.text.strip()]
    if extras_text:
        lines.append("**Additional**:")
        lines.extend(f"- {text}" for text in extras_text)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _filter_by_track(items: list[Bullet] | list[SkillCategory], track: str) -> list:
    """Shared-unless-tagged filter for Bullet/SkillCategory: an item with no
    `tracks` is included on every track's base CV; one with a `tracks` list
    is included only when `track` is in it."""
    return [item for item in items if not item.tracks or track in item.tracks]


def _visible_roles_for_track(core: Core, track: str) -> list[Role]:
    """Role-level visibility gate for base_cv (step 2d): same shared-unless-
    tagged contract as `_filter_by_track`, but this ONLY controls base_cv —
    render_profile_md() and the employers.history projection in
    render_candidate_yaml() deliberately ignore Role.tracks and always
    include every role (they are facts, not a track's presentation)."""
    return [role for role in core.roles if not role.tracks or track in role.tracks]


def _variant_skills(profile: Profile, track: str) -> list[SkillCategory]:
    """A variant with its OWN `skills` list wins wholesale (M0b: the owner's
    per-track skills are full lists with their own labels, not a reordering
    of core's) — otherwise core.skills filtered by tag."""
    variant = profile.variants.get(track)
    if variant is not None and variant.skills:
        return variant.skills
    return _filter_by_track(profile.core.skills, track)


def _role_bullets_for_track(role: Role, track: str) -> list[str]:
    """Role.bullets_by_track is a full per-track REWRITE (M0b: different
    wording, different count, not a filtered subset) — it wins wholesale
    when present; otherwise fall back to filtering core `bullets` by tag."""
    override = role.bullets_by_track.get(track)
    if override is not None:
        return override
    return [b.text for b in _filter_by_track(role.bullets, track) if b.text.strip()]


def render_base_cv(profile: Profile, track: str) -> str:
    """Render `base_cv_<track>.md`: track-filtered bullets/skills/roles, plus
    the variant's own headline/summary (falling back to core when absent) and
    `notes` — a free-text behavioral instruction rendered as the very first
    block, before the headline (step 2d)."""
    core = profile.core
    variant = profile.variants.get(track)
    headline = (variant.headline if variant else "") or core.identity.headline
    summary = (variant.summary if variant else "") or core.summary
    skills = _variant_skills(profile, track)

    lines: list[str] = []
    variant_notes = (variant.notes if variant else "").strip()
    if variant_notes:
        lines.append(variant_notes)
        lines.append("")
    if headline.strip():
        lines.append(f"# {headline.strip()}")
        lines.append("")
    if summary.strip():
        lines.append(summary.strip())
        lines.append("")

    if skills:
        lines.append("## Skills")
        lines.append("")
        for category in skills:
            if category.items:
                lines.append(f"**{category.category}**: {', '.join(category.items)}")
        lines.append("")

    visible_roles = _visible_roles_for_track(core, track)
    if visible_roles:
        lines.append("## Experience")
        lines.append("")
        for i, role in enumerate(visible_roles, start=1):
            title = role.title_by_track.get(track, role.title)
            subtitle = role.subtitle_by_track.get(track, role.subtitle)
            stack_line = role.stack_line_by_track.get(track, role.stack_line)
            lines.append(f"### Role {i} — {role.company} ({role.period})")
            lines.append("")
            head = f"**{title}**"
            if subtitle.strip():
                head += f" | {subtitle.strip()}"
            lines.append(head)
            lines.append("")
            for text in _role_bullets_for_track(role, track):
                lines.append(f"- {text}")
            if stack_line.strip():
                lines.append(f"\nStack: {stack_line.strip()}.")
            lines.append("")

    if core.education.entries:
        lines.append("## Education & Courses")
        lines.append("")
        for entry in core.education.entries:
            if entry.text.strip():
                lines.append(f"- {entry.text}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def render_all(profile: Profile, out_dir: Path) -> list[Path]:
    """Render + write candidate.yaml, candidate_profile.md, one
    base_cv_<track>.md per variant key, and — only when non-empty —
    generation_rules.local.md (the wave-2 optional prompt tail). Always a
    full overwrite, never a merge. Returns the paths actually written.

    "Full overwrite" also means a file this render no longer needs is
    REMOVED, not left behind: a base_cv_<track>.md for a track dropped from
    `variants`, or a generation_rules.local.md left over from a
    generation_notes that has since been cleared. Without this, a stale
    generation_rules.local.md keeps being spliced into every future
    generation prompt (hunter/gen_prompt.py::_local_tail only checks the
    file's existence, not whether the current profile still wants it)."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    candidate_yaml_path = out_dir / "candidate.yaml"
    candidate_yaml_path.write_text(render_candidate_yaml(profile), encoding="utf-8")
    written.append(candidate_yaml_path)

    profile_md_path = out_dir / "candidate_profile.md"
    profile_md_path.write_text(render_profile_md(profile), encoding="utf-8")
    written.append(profile_md_path)

    current_base_cv_names = {f"base_cv_{track}.md" for track in profile.variants}
    for stale_base_cv in out_dir.glob("base_cv_*.md"):
        if stale_base_cv.name not in current_base_cv_names:
            stale_base_cv.unlink()

    for track in profile.variants:
        base_cv_path = out_dir / f"base_cv_{track}.md"
        base_cv_path.write_text(render_base_cv(profile, track), encoding="utf-8")
        written.append(base_cv_path)

    generation_notes = profile.core.generation_notes.strip()
    local_tail_path = out_dir / "generation_rules.local.md"
    if generation_notes:
        local_tail_path.write_text(generation_notes + "\n", encoding="utf-8")
        written.append(local_tail_path)
    elif local_tail_path.exists():
        local_tail_path.unlink()

    return written
