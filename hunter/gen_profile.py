"""Per-user generation profile loader (docs/GENERATION_ARCHITECTURE_ANALYSIS.md
§6 wave 3): the knobs that decide WHAT the pipeline writes into a CV —
ATS-loop thresholds, verdict/refine rounds, gate modes, document layout —
as opposed to HOW the system is wired (models, timeouts, schedule), which
stays in `.env`/`hunter/config.py`.

Same architecture as `hunter/filter_profile.py`, copied deliberately rather
than reinvented:

Layer 1 — ``builtin_defaults()``: today's hardcoded values, one shared copy,
code-reviewed. Every default here reproduces a constant or an env default
that already existed before this module — this wave changes nothing about
runtime behavior by itself.

Layer 2 — optional user YAML (``candidate/generation.yaml``, or
``users/{uid}/candidate/generation.yaml`` once multi-user). Missing file =>
Layer 1 as-is, byte-for-byte.

Layer 3 — env var override, for the subset of keys that already had an env
var before this module existed (``ATS_VERDICT_TARGET``, ``JUDGE_MODE``, ...).
A brand-new YAML-only knob has no env fallback by design — env stays the
emergency lever it always was, not a second way to set every new knob.

Priority: env > YAML > builtin.

Cache: the YAML-merge result is cached by ``(path, mtime_ns)`` so an external
edit is picked up without a restart, exactly like filter_profile. Env
overrides are NOT part of the cache key — they're re-applied on every
``load_gen_profile()``/``get()`` call so a changed env var takes effect
immediately, independent of any file's mtime (relevant for tests that
monkeypatch os.environ without touching a file).

``hunter/config.py`` reads these values through ``get()`` at import time —
the apply pipeline is a fresh subprocess per vacancy (see hunter.candidate's
own docstring for the same reasoning), so import-time resolution is correct
there. A constant read INSIDE a function body elsewhere (hunter/pipeline/
ats.py, hunter/verdict_refine.py, hunter/ats_pdf_roundtrip.py, hunter/
pipeline/gates.py) must call ``get()`` at call time, not stash the value as a
def-time default — see ``hunter.filters._resolve_flt``'s docstring for why a
default-arg snapshot breaks monkeypatching / same-process reload.
"""

from __future__ import annotations

import copy
import logging
import os
from pathlib import Path
from typing import Any, Callable

import yaml

logger = logging.getLogger(__name__)


def builtin_defaults() -> dict[str, Any]:
    """Layer 1 — today's values verbatim, WHY-comments point at their origin."""
    return {
        "ats": {
            # hunter/pipeline/ats.py::_ATS_THRESHOLD — combined score to pass
            # the deterministic keyword loop without a rewrite.
            "threshold": 95.0,
            # hunter/pipeline/ats.py::_ATS_MAX_ROUNDS — honest rewrite rounds
            # before the loop escalates to a soft, then aggressive, pass.
            "honest_rounds": 2,
            # local _TOTAL_ROUNDS in _ats_check_loop — total rewrite rounds
            # (honest + soft + aggressive) before the loop gives up and keeps
            # whatever score the last rewrite reached.
            "total_rounds": 5,
            # hunter/pipeline/ats.py::_ATS_CHECKLIST_CAP — max keywords handed
            # to the FIRST generation prompt as a deterministic checklist.
            "checklist_cap": 30,
        },
        "verdict": {
            "enabled": True,  # config ATS_VERDICT_ENABLED
            "target": 95.0,  # config ATS_VERDICT_TARGET
            "max_refines": 5,  # config ATS_VERDICT_MAX_REFINES
            # hunter/verdict_refine.py::STRETCH_FROM_ROUND — first refine
            # round allowed to add posting technologies absent from the
            # candidate profile (rounds below this stay honest/visibility-only).
            "stretch_from_round": 4,
            # hunter/ats_pdf_roundtrip.py::HEAL_DELTA_PP — trigger the NBSP
            # self-heal pass when the rendered-PDF score is this many
            # percentage points below the JSON score.
            "heal_delta_pp": 5.0,
        },
        "judge": {
            "enabled": True,  # config JUDGE_ENABLED
            "mode": "warn",  # config JUDGE_MODE — report | warn | block
            "max_repair_rounds": 1,  # config JUDGE_MAX_REPAIR_ROUNDS
        },
        "gates": {
            "doomed_enabled": True,  # config DOOMED_GATE_ENABLED
            "doomed_hard_action": "skip",  # config DOOMED_GATE_HARD_ACTION
            "prescreen_enabled": True,  # config PRESCREEN_ENABLED
            "prescreen_mode": "warn",  # config PRESCREEN_MODE
            "prescreen_min_confidence": 0.9,  # config PRESCREEN_MIN_CONFIDENCE
            "repost_enabled": True,  # config REPOST_GATE_ENABLED
            "repost_window_days": 60,  # config REPOST_WINDOW_DAYS
            # hunter/pipeline/gates.py::_REACT_SKIP_MIN_MENTIONS — minimum
            # "react" mentions (with zero "angular" mentions) to auto-skip
            # pre-LLM without calling the model at all.
            "react_skip_min_mentions": 3,
        },
        "generation": {
            "skip_pl_for_en": True,  # config GEN_SKIP_PL_FOR_EN
        },
    }


# ── Validation specs for the flat (non-document) sections ───────────────────
# dotpath -> {"type": bool|int|float|str, ["min"], ["max"], ["choices"]}.
# A key absent here is "unknown" when seen in a user YAML file, and is
# ignored with a warning rather than merged.
_KEY_SPECS: dict[str, dict[str, Any]] = {
    "ats.threshold": {"type": "float", "min": 0.0, "max": 100.0},
    "ats.honest_rounds": {"type": "int", "min": 0},
    "ats.total_rounds": {"type": "int", "min": 0},
    "ats.checklist_cap": {"type": "int", "min": 0},
    "verdict.enabled": {"type": "bool"},
    "verdict.target": {"type": "float", "min": 0.0, "max": 100.0},
    "verdict.max_refines": {"type": "int", "min": 0},
    "verdict.stretch_from_round": {"type": "int", "min": 0},
    "verdict.heal_delta_pp": {"type": "float", "min": 0.0},
    "judge.enabled": {"type": "bool"},
    "judge.mode": {"type": "str", "choices": ("report", "warn", "block")},
    "judge.max_repair_rounds": {"type": "int", "min": 0},
    "gates.doomed_enabled": {"type": "bool"},
    "gates.doomed_hard_action": {"type": "str", "choices": ("skip", "warn")},
    "gates.prescreen_enabled": {"type": "bool"},
    "gates.prescreen_mode": {"type": "str", "choices": ("report", "warn", "skip")},
    "gates.prescreen_min_confidence": {"type": "float", "min": 0.0, "max": 1.0},
    "gates.repost_enabled": {"type": "bool"},
    "gates.repost_window_days": {"type": "int", "min": 0},
    "gates.react_skip_min_mentions": {"type": "int", "min": 0},
    "generation.skip_pl_for_en": {"type": "bool"},
}

# Sections handled by a dedicated merge function instead of the generic
# flat-leaf validator above (PR 2 adds "document": heterogeneous sub-shapes —
# dicts, lists, list-of-dict — that a single {type, min, max, choices} spec
# can't describe). Populated by the section itself; empty until PR 2.
_SECTION_MERGERS: dict[str, Callable[[dict[str, Any], Any, str], None]] = {}


def _cast_bool(raw: str) -> bool:
    return raw.strip().lower() in ("true", "1", "yes")


def _cast_int(raw: str) -> int:
    return int(raw)


def _cast_float(raw: str) -> float:
    return float(raw)


def _cast_str_lower(raw: str) -> str:
    return raw.strip().lower()


# dotpath -> (ENV_VAR_NAME, caster). Only keys that had an env var BEFORE
# this module existed are listed — see the module docstring.
_ENV_OVERRIDES: dict[str, tuple[str, Callable[[str], Any]]] = {
    "verdict.enabled": ("ATS_VERDICT_ENABLED", _cast_bool),
    "verdict.target": ("ATS_VERDICT_TARGET", _cast_float),
    "verdict.max_refines": ("ATS_VERDICT_MAX_REFINES", _cast_int),
    "judge.enabled": ("JUDGE_ENABLED", _cast_bool),
    "judge.mode": ("JUDGE_MODE", _cast_str_lower),
    "judge.max_repair_rounds": ("JUDGE_MAX_REPAIR_ROUNDS", _cast_int),
    "gates.doomed_enabled": ("DOOMED_GATE_ENABLED", _cast_bool),
    "gates.doomed_hard_action": ("DOOMED_GATE_HARD_ACTION", _cast_str_lower),
    "gates.prescreen_enabled": ("PRESCREEN_ENABLED", _cast_bool),
    "gates.prescreen_mode": ("PRESCREEN_MODE", _cast_str_lower),
    "gates.prescreen_min_confidence": ("PRESCREEN_MIN_CONFIDENCE", _cast_float),
    "gates.repost_enabled": ("REPOST_GATE_ENABLED", _cast_bool),
    "gates.repost_window_days": ("REPOST_WINDOW_DAYS", _cast_int),
    "generation.skip_pl_for_en": ("GEN_SKIP_PL_FOR_EN", _cast_bool),
}

# (path, mtime_ns) -> profile, WITHOUT env overrides applied (see module
# docstring — env is re-applied fresh on every load, not cached).
_cache: dict[tuple[str, int | None], dict[str, Any]] = {}


def clear_gen_profile_cache() -> None:
    """Drop the load_gen_profile cache (tests / forced reload)."""
    _cache.clear()


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _resolve_path(path: str | Path | None = None) -> Path:
    if path is not None:
        return Path(path)
    env = os.environ.get("GENERATION_YAML_PATH")
    if env:
        return Path(env)
    # Single-user default: generation.yaml next to candidate.yaml, same
    # resolution order as filter_profile._resolve_path. Per-user apply
    # subprocesses already get CANDIDATE_YAML_PATH from hunter.users.user_env,
    # so per-user generation.yaml works with zero changes there.
    cand_env = os.environ.get("CANDIDATE_YAML_PATH")
    if cand_env:
        return Path(cand_env).expanduser().resolve().parent / "generation.yaml"
    return Path(__file__).resolve().parent.parent / "candidate" / "generation.yaml"


def _set_nested(d: dict[str, Any], dotpath: str, value: Any) -> None:
    parts = dotpath.split(".")
    node = d
    for part in parts[:-1]:
        nxt = node.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            node[part] = nxt
        node = nxt
    node[parts[-1]] = value


def _validate_leaf(dotpath: str, value: Any, *, source: str) -> tuple[bool, Any]:
    """Type/range-check one flat-section value. Returns (ok, normalized)."""
    spec = _KEY_SPECS[dotpath]
    kind = spec["type"]
    if kind == "bool":
        if not isinstance(value, bool):
            logger.warning(
                "generation profile %s: %s must be a bool, got %r — kept default",
                source,
                dotpath,
                value,
            )
            return False, None
        return True, value
    if kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            logger.warning(
                "generation profile %s: %s must be an int, got %r — kept default",
                source,
                dotpath,
                value,
            )
            return False, None
        if "min" in spec and value < spec["min"]:
            logger.warning(
                "generation profile %s: %s=%r below minimum %s — kept default",
                source,
                dotpath,
                value,
                spec["min"],
            )
            return False, None
        return True, value
    if kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            logger.warning(
                "generation profile %s: %s must be a number, got %r — kept default",
                source,
                dotpath,
                value,
            )
            return False, None
        fvalue = float(value)
        if "min" in spec and fvalue < spec["min"]:
            logger.warning(
                "generation profile %s: %s=%r below minimum %s — kept default",
                source,
                dotpath,
                value,
                spec["min"],
            )
            return False, None
        if "max" in spec and fvalue > spec["max"]:
            logger.warning(
                "generation profile %s: %s=%r above maximum %s — kept default",
                source,
                dotpath,
                value,
                spec["max"],
            )
            return False, None
        return True, fvalue
    if kind == "str":
        if not isinstance(value, str):
            logger.warning(
                "generation profile %s: %s must be a string, got %r — kept default",
                source,
                dotpath,
                value,
            )
            return False, None
        normalized = value.strip().lower()
        choices = spec.get("choices")
        if choices and normalized not in choices:
            logger.warning(
                "generation profile %s: %s=%r must be one of %s — kept default",
                source,
                dotpath,
                value,
                choices,
            )
            return False, None
        return True, normalized
    # Unreachable with the specs above; never raise from the loader.
    return False, None


def _merge_user(profile: dict[str, Any], user: dict[str, Any], *, source: str) -> None:
    if not isinstance(user, dict):
        logger.warning(
            "generation profile %s: root must be a mapping, got %s — ignored",
            source,
            type(user).__name__,
        )
        return
    for section, leaves in user.items():
        if section in _SECTION_MERGERS:
            _SECTION_MERGERS[section](profile, leaves, source)
            continue
        if not isinstance(leaves, dict):
            logger.warning(
                "generation profile %s: section %r must be a mapping — ignored",
                source,
                section,
            )
            continue
        for leaf, value in leaves.items():
            dotpath = f"{section}.{leaf}"
            if dotpath not in _KEY_SPECS:
                logger.warning(
                    "generation profile %s: unknown key %r — ignored",
                    source,
                    dotpath,
                )
                continue
            ok, normalized = _validate_leaf(dotpath, value, source=source)
            if ok:
                _set_nested(profile, dotpath, normalized)


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as exc:  # noqa: BLE001 — never raise from the loader
        logger.warning("generation profile %s: failed to read (%s) — using builtins", path, exc)
        return {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        logger.warning(
            "generation profile %s: root must be a mapping, got %s — using builtins",
            path,
            type(data).__name__,
        )
        return {}
    return data


def _apply_env_overrides(profile: dict[str, Any]) -> None:
    for dotpath, (env_name, caster) in _ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is None:
            continue
        try:
            value = caster(raw)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "generation profile: env %s=%r invalid for %s (%s) — ignored",
                env_name,
                raw,
                dotpath,
                exc,
            )
            continue
        _set_nested(profile, dotpath, value)


def _build_profile(path: Path) -> dict[str, Any]:
    """Layer 1 + Layer 2 only — env overrides are applied per-call, not cached."""
    profile = builtin_defaults()
    if path.exists():
        user = _load_yaml(path)
        if user:
            _merge_user(profile, user, source=str(path))
    return profile


def load_gen_profile(path: str | Path | None = None) -> dict[str, Any]:
    """Deep-copy Layer 1, merge user YAML (if any) with caching by path+mtime,
    then apply env overrides fresh on every call (Layer 3 — never cached, so
    a changed env var takes effect immediately regardless of file state).

    ``path=None`` resolves ``GENERATION_YAML_PATH`` / per-user
    ``CANDIDATE_YAML_PATH`` sibling / default ``candidate/generation.yaml``.
    """
    resolved = _resolve_path(path)
    key = (str(resolved.resolve()), _mtime_ns(resolved))
    cached = _cache.get(key)
    if cached is None:
        cached = _build_profile(resolved)
        _cache[key] = cached
    profile = copy.deepcopy(cached)
    _apply_env_overrides(profile)
    return profile


def get(dotpath: str, default: Any = None) -> Any:
    """Read a nested key with dot notation, e.g. get("ats.threshold")."""
    node: Any = load_gen_profile()
    for part in dotpath.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node if node is not None else default
