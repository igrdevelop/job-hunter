"""hunter/profile_schema.py — the structured resume-profile document.

docs/RESUME_PROFILE_STORE_PLAN.md: the canonical store for a candidate's
identity/career data is a single JSON document (schema_version 1), of which
candidate.yaml / candidate_profile.md / base_cv_<track>.md become a
deterministic render (hunter/profile_render.py). This module owns the shape
only — dataclasses, a tolerant `from_dict`, `to_dict`, and `validate`.

`core.roles` is a superset of a wave-2 `employers.history` entry (docs/
GENERATION_ARCHITECTURE_ANALYSIS.md §6, hunter/gen_prompt.py): it carries the
same company/title/period/backend/bullets_max/legacy_stack_ok/title_by_track
fields PLUS the narrative (`description`, `bullets`) that history entries
never had. The renderer projects `core.roles` down to `employers.history`.

Tolerant by design: `from_dict` never raises on malformed input — an unknown
key is dropped with a logged warning, a wrong-shaped value falls back to the
field's default. A resume upload is untrusted input from a parser (itself
fed by an LLM); the profile store must degrade gracefully, not crash the
onboarding flow. `validate()` is the separate, explicit check for "is this
document actually renderable" — callers decide what to do with its findings.
"""

from __future__ import annotations

import dataclasses
import logging
from dataclasses import dataclass, field, fields
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

# `origin` on Role/Bullet/SkillCategory/Extra is one of these two literals.
# Kept as plain `str` (not typing.Literal) to match this codebase's existing
# soft-typing style (see hunter/prescreen.py's PrescreenVerdict) and because
# from_dict must accept — and simply not trust — any string a parser wrote.
ORIGIN_PARSED = "parsed"
ORIGIN_EDITED = "edited"


# ── Dataclasses ────────────────────────────────────────────────────────────


@dataclass
class Bullet:
    """One narrative achievement line under a role.

    `tracks` is an optional include-filter for the simple case: an empty
    list means the bullet is shared by every track's base CV. A role that
    needs an actual per-track REWRITE (different wording, different count —
    see Role.bullets_by_track) overrides this filtering wholesale for that
    track; `tracks` only matters where no such override exists.
    """

    text: str = ""
    origin: str = ORIGIN_PARSED
    tracks: list[str] = field(default_factory=list)


@dataclass
class Identity:
    full_name: str = ""
    aka: str = ""
    headline: str = ""
    contact: str = ""
    cv_filename_prefix: str = ""


@dataclass
class Location:
    home_city: str = ""
    home_city_aliases: list[str] = field(default_factory=list)
    acceptable_hybrid: list[str] = field(default_factory=list)
    weekly_hybrid: list[str] = field(default_factory=list)
    work_authorization: str = ""


@dataclass
class Languages:
    spoken: list[str] = field(default_factory=list)
    cv_languages: list[str] = field(default_factory=list)
    disqualify_required: list[str] = field(default_factory=list)


@dataclass
class FlexibleEmployer:
    name: str = ""
    period: str = ""
    projects: list[str] = field(default_factory=list)


@dataclass
class Employers:
    """`real_companies` / `profile_titles` / `history` are deliberately NOT
    stored here — M0b decided they are render-time derivations of
    `protected` + `flexible.name` and of `Core.roles` (see
    hunter/profile_render.py), so there is exactly one place to edit an
    employer name instead of three hand-synced copies."""

    protected: list[str] = field(default_factory=list)
    flexible: FlexibleEmployer = field(default_factory=FlexibleEmployer)


@dataclass
class EducationEntry:
    text: str = ""
    origin: str = ORIGIN_PARSED


@dataclass
class Education:
    entries: list[EducationEntry] = field(default_factory=list)
    school_keyword: str = ""
    expected_role_count: int = 0


@dataclass
class Experience:
    years_label: str = ""
    since_year: int = 0


@dataclass
class Role:
    id: str = ""
    company: str = ""
    title: str = ""
    period: str = ""
    subtitle: str = ""
    description: str = ""
    backend: str = ""
    bullets_max: str = ""
    legacy_stack_ok: bool = False
    title_by_track: dict[str, str] = field(default_factory=dict)
    subtitle_by_track: dict[str, str] = field(default_factory=dict)
    stack_line: str = ""
    stack_line_by_track: dict[str, str] = field(default_factory=dict)
    bullets: list[Bullet] = field(default_factory=list)
    # Full per-track REPLACEMENT of the rendered base-CV bullet list for this
    # role (M0b finding: the owner's real per-track bullets are rewrites,
    # not a filtered subset — different wording, different count). Absent
    # for a track ⇒ the renderer falls back to filtering `bullets` by their
    # own `tracks` tag.
    bullets_by_track: dict[str, list[str]] = field(default_factory=dict)
    # Presentation-only visibility gate (docs/RESUME_PROFILE_STORE_PLAN.md
    # step 2d): an empty list means the role is visible on every track's
    # base CV (same shared-unless-tagged contract as Bullet.tracks); a
    # non-empty list hides the role entirely from any track not listed.
    # Deliberately NOT read by render_profile_md() or the employers.history
    # projection — those are facts (candidate_profile.md's narrative superset,
    # the judge's RED LINES ground truth), and a role hidden from one track's
    # CV is still a real, true fact about the candidate's history.
    tracks: list[str] = field(default_factory=list)
    origin: str = ORIGIN_PARSED


@dataclass
class SkillCategory:
    category: str = ""
    items: list[str] = field(default_factory=list)
    origin: str = ORIGIN_PARSED
    # Same shared-unless-tagged semantics as Bullet.tracks, for a whole
    # category (e.g. an "AI Tools" category that only makes sense on the
    # `ai` track). A variant that defines its own `skills` list overrides
    # this filtering wholesale, same precedence as Role.bullets_by_track.
    tracks: list[str] = field(default_factory=list)


@dataclass
class Extra:
    kind: str = "other"  # certification | link | award | other
    text: str = ""
    origin: str = ORIGIN_PARSED


@dataclass
class Core:
    identity: Identity = field(default_factory=Identity)
    location: Location = field(default_factory=Location)
    languages: Languages = field(default_factory=Languages)
    employers: Employers = field(default_factory=Employers)
    education: Education = field(default_factory=Education)
    experience: Experience = field(default_factory=Experience)
    summary: str = ""
    roles: list[Role] = field(default_factory=list)
    skills: list[SkillCategory] = field(default_factory=list)
    extras: list[Extra] = field(default_factory=list)
    # Optional free-text prompt tail (wave 2's "story bank" —
    # candidate/generation_rules.local.md). Renders verbatim when non-empty.
    generation_notes: str = ""


@dataclass
class Variant:
    """A track "personality": a delta of presentation over `core`, never a
    second copy of the facts. Absent fields fall back to core (headline/
    summary) or to core.skills filtered by tag (skills — see SkillCategory
    .tracks above)."""

    headline: str = ""
    summary: str = ""
    skills: list[SkillCategory] = field(default_factory=list)
    # Free-text behavioral instruction for THIS track's base_cv, rendered
    # verbatim as the file's first block, before the headline (step 2d) —
    # e.g. the react track's "never write 'Angular' in a role title" rule,
    # which had no home in the schema before this and was silently lost on
    # render (see the #239 review finding).
    notes: str = ""


@dataclass
class Leftover:
    """A raw fragment the parser could not confidently place anywhere."""

    text: str = ""
    source_upload_id: str = ""


@dataclass
class Upload:
    id: str = ""
    filename: str = ""
    sha256: str = ""
    parsed_at: str = ""


@dataclass
class Profile:
    schema_version: int = SCHEMA_VERSION
    core: Core = field(default_factory=Core)
    variants: dict[str, Variant] = field(default_factory=dict)
    leftovers: list[Leftover] = field(default_factory=list)
    uploads: list[Upload] = field(default_factory=list)


# ── Tolerant construction ───────────────────────────────────────────────────


def _warn_unknown_keys(data: dict, known: set[str], where: str) -> None:
    unknown = set(data) - known
    if unknown:
        logger.warning("profile_schema: ignoring unknown key(s) in %s: %s", where, sorted(unknown))


def _coerce_field(current_default: Any, raw: Any) -> Any:
    """Coerce `raw` to the shape of `current_default` (a field's own default
    value, which pins its expected type). Bad-shaped input falls back to the
    default rather than raising — see the module docstring."""
    if isinstance(current_default, bool):
        return bool(raw) if raw is not None else current_default
    if isinstance(current_default, int):
        try:
            return int(raw) if raw is not None else current_default
        except (TypeError, ValueError):
            return current_default
    if isinstance(current_default, str):
        return str(raw) if raw is not None else current_default
    if isinstance(current_default, list):
        return [str(x) for x in raw] if isinstance(raw, list) else list(current_default)
    if isinstance(current_default, dict):
        return dict(raw) if isinstance(raw, dict) else dict(current_default)
    return raw if raw is not None else current_default


def _build_flat(cls: type, data: Any, where: str, overrides: frozenset[str] = frozenset()) -> Any:
    """Build a "flat" dataclass (only str/bool/int/list[str]/dict[str,str]
    fields, plus any field named in `overrides` which the caller fills in
    separately) from a dict. Warns on unknown keys, defaults on bad shapes,
    never raises."""
    d = data if isinstance(data, dict) else {}
    field_names = {f.name for f in fields(cls)}
    _warn_unknown_keys(d, field_names, where)
    kwargs = {}
    for f in fields(cls):
        if f.name in overrides:
            continue
        default = f.default_factory() if f.default_factory is not dataclasses.MISSING else f.default
        kwargs[f.name] = _coerce_field(default, d.get(f.name))
    return cls(**kwargs)


def _str_dict(raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if isinstance(v, (str, int, float))}


def _str_list_dict(raw: Any) -> dict[str, list[str]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, list[str]] = {}
    for k, v in raw.items():
        if isinstance(v, list):
            out[str(k)] = [str(x) for x in v]
    return out


def _bullet_from_dict(data: Any, where: str) -> Bullet:
    return _build_flat(Bullet, data, where)


_ROLE_DICT_FIELDS = frozenset(
    {"bullets", "bullets_by_track", "title_by_track", "subtitle_by_track", "stack_line_by_track"}
)


def _role_from_dict(data: Any, index: int) -> Role:
    d = data if isinstance(data, dict) else {}
    where = f"core.roles[{index}]"
    role = _build_flat(Role, d, where, overrides=_ROLE_DICT_FIELDS)
    raw_bullets = d.get("bullets")
    if isinstance(raw_bullets, list):
        role.bullets = [
            _bullet_from_dict(item, f"{where}.bullets[{i}]") for i, item in enumerate(raw_bullets)
        ]
    role.bullets_by_track = _str_list_dict(d.get("bullets_by_track"))
    role.title_by_track = _str_dict(d.get("title_by_track"))
    role.subtitle_by_track = _str_dict(d.get("subtitle_by_track"))
    role.stack_line_by_track = _str_dict(d.get("stack_line_by_track"))
    return role


def _skill_category_from_dict(data: Any, where: str) -> SkillCategory:
    return _build_flat(SkillCategory, data, where)


def _employers_from_dict(data: Any) -> Employers:
    d = data if isinstance(data, dict) else {}
    employers = _build_flat(Employers, d, "core.employers", overrides=frozenset({"flexible"}))
    employers.flexible = _build_flat(FlexibleEmployer, d.get("flexible"), "core.employers.flexible")
    return employers


def _education_from_dict(data: Any) -> Education:
    d = data if isinstance(data, dict) else {}
    education = _build_flat(Education, d, "core.education", overrides=frozenset({"entries"}))
    raw_entries = d.get("entries")
    if isinstance(raw_entries, list):
        education.entries = [
            _build_flat(EducationEntry, item, f"core.education.entries[{i}]")
            for i, item in enumerate(raw_entries)
        ]
    return education


_CORE_NESTED_FIELDS = frozenset(
    {
        "identity",
        "location",
        "languages",
        "employers",
        "education",
        "experience",
        "roles",
        "skills",
        "extras",
    }
)


def _core_from_dict(data: Any) -> Core:
    d = data if isinstance(data, dict) else {}
    core = _build_flat(Core, d, "core", overrides=_CORE_NESTED_FIELDS)
    core.identity = _build_flat(Identity, d.get("identity"), "core.identity")
    core.location = _build_flat(Location, d.get("location"), "core.location")
    core.languages = _build_flat(Languages, d.get("languages"), "core.languages")
    core.employers = _employers_from_dict(d.get("employers"))
    core.education = _education_from_dict(d.get("education"))
    core.experience = _build_flat(Experience, d.get("experience"), "core.experience")

    raw_roles = d.get("roles")
    core.roles = (
        [_role_from_dict(item, i) for i, item in enumerate(raw_roles)]
        if isinstance(raw_roles, list)
        else []
    )
    raw_skills = d.get("skills")
    core.skills = (
        [_skill_category_from_dict(item, f"core.skills[{i}]") for i, item in enumerate(raw_skills)]
        if isinstance(raw_skills, list)
        else []
    )
    raw_extras = d.get("extras")
    core.extras = (
        [_build_flat(Extra, item, f"core.extras[{i}]") for i, item in enumerate(raw_extras)]
        if isinstance(raw_extras, list)
        else []
    )
    return core


def _variant_from_dict(data: Any, key: str) -> Variant:
    d = data if isinstance(data, dict) else {}
    where = f"variants.{key}"
    variant = _build_flat(Variant, d, where, overrides=frozenset({"skills"}))
    raw_skills = d.get("skills")
    variant.skills = (
        [
            _skill_category_from_dict(item, f"{where}.skills[{i}]")
            for i, item in enumerate(raw_skills)
        ]
        if isinstance(raw_skills, list)
        else []
    )
    return variant


_PROFILE_NESTED_FIELDS = frozenset({"core", "variants", "leftovers", "uploads"})


def from_dict(data: dict) -> Profile:
    """Tolerant construction: unknown keys are dropped with a warning,
    bad-shaped values fall back to field defaults. Never raises."""
    d = data if isinstance(data, dict) else {}
    _warn_unknown_keys(d, {"schema_version", *_PROFILE_NESTED_FIELDS}, "<root>")

    profile = Profile()
    try:
        profile.schema_version = int(d.get("schema_version") or SCHEMA_VERSION)
    except (TypeError, ValueError):
        profile.schema_version = SCHEMA_VERSION

    profile.core = _core_from_dict(d.get("core"))

    raw_variants = d.get("variants")
    if isinstance(raw_variants, dict):
        profile.variants = {str(k): _variant_from_dict(v, str(k)) for k, v in raw_variants.items()}

    raw_leftovers = d.get("leftovers")
    profile.leftovers = (
        [_build_flat(Leftover, item, f"leftovers[{i}]") for i, item in enumerate(raw_leftovers)]
        if isinstance(raw_leftovers, list)
        else []
    )

    raw_uploads = d.get("uploads")
    profile.uploads = (
        [_build_flat(Upload, item, f"uploads[{i}]") for i, item in enumerate(raw_uploads)]
        if isinstance(raw_uploads, list)
        else []
    )
    return profile


def to_dict(profile: Profile) -> dict:
    """Plain-dict serialization, JSON-ready. Round-trips through `from_dict`."""
    return dataclasses.asdict(profile)


# ── Validation ───────────────────────────────────────────────────────────


def validate(profile: Profile) -> list[str]:
    """Human-readable list of problems; empty = safe to render. Mirrors
    hunter.candidate.REQUIRED_IDENTITY_FIELDS — a document that fails this
    cannot produce a candidate.yaml that passes candidate.require_identity()."""
    problems: list[str] = []
    identity = profile.core.identity
    if not identity.full_name.strip():
        problems.append("core.identity.full_name is required")
    if not identity.contact.strip():
        problems.append("core.identity.contact is required")
    if not identity.cv_filename_prefix.strip():
        problems.append("core.identity.cv_filename_prefix is required")
    return problems
