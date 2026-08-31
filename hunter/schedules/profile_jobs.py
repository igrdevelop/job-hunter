"""hunter/schedules/profile_jobs.py — drain the resume profile store's
render/parse job queue (docs/RESUME_PROFILE_STORE_PLAN.md step 4b).

Runs every ~20s (see hunter/schedules/__init__.py::register). Each tick:
reset any 'running' row stuck for more than STALE_TIMEOUT_MIN minutes (a
drain tick that crashed mid-job), then drain every 'pending' row to
completion — at this volume a queue emptied within one tick is the common
case, and a slow parse call blocking the next tick by a few seconds is an
acceptable trade for not needing real concurrency here (M2/parallel workers
is exactly this decision for the apply queue too — see
docs/HUNT_APPLY_SPLIT_PLAN.md).

kind='render': payload is the FULL profile JSON (self-contained — this repo
never reads the API's app.sqlite). Renders into users/{user_id}/candidate/
via hunter.profile_render.render_all, AND writes profile.json there too —
wave-2 groundwork for a future consumer that reads the structure directly
instead of the three rendered files (see hunter/profile_render.py's module
docstring).

kind='parse': payload is a path RELATIVE to users/{user_id}/ (e.g.
"uploads/<uuid>.docx") — validated to stay inside the user's own directory
before any file is touched, since it originates from another process (the
API) over a shared DB row and must not be trusted at face value. Extracts +
parses into a draft Profile; the parser's own fallback covers a missing or
failing LLM call (hunter/profile_parse.py never hard-fails).

kind='preview' (docs/PROFILE_PAGE_TABS_WORKORDER.md, the bot-repo work
item): payload is JSON `{"profile": <full profile document>, "track": "<key
or 'core'>"}` — self-contained like 'render', the bot never reads the API's
app.sqlite. Deterministic, $0, NO LLM call anywhere (hunter.profile_preview
owns that contract). Renders a generic no-vacancy CV via generate_docs.py
(--no-tracker: never a tracker row, never Sheets/Drive/Telegram delivery —
a preview is not an application) into its own dated subfolder
users/{user_id}/candidate/preview/<track>/<UTC timestamp>/ — each run gets a
fresh folder, kept as history, never overwritten (owner decision
2026-08-31). `track` is validated against the same simple-slug shape a
'parse' path is (hunter.profile_preview.validate_track) before it is ever
used as a path component. Requires the user's candidate.yaml to already
exist (i.e. the profile was published/rendered at least once) — the PDF's
identity comes from that file, not from the payload's own core.identity, so
a preview before the first publish fails with a clear "publish first"
message instead of half-rendering under a placeholder identity.

Any failure — bad JSON, an unsafe path/track, an extraction error, a
generate_docs.py failure, an unexpected exception — calls fail_profile_job()
with the error message; the job is terminal, and a retry is a new
PUT/upload/preview-request from the client, not a bot-side mechanism. The
scheduled tick itself is wrapped in best_effort("profile.jobs") so repeated
DRAIN failures (as opposed to one user's bad upload/profile, handled
per-job above) alert instead of degrading silently forever.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

STALE_TIMEOUT_MIN = 10

KIND_RENDER = "render"
KIND_PARSE = "parse"
KIND_PREVIEW = "preview"


def _resolve_user_relative_path(user_id: str, relpath: str) -> Path:
    """Resolve a 'parse' job's payload — a path relative to
    users/{user_id}/ — to an absolute path, refusing anything that could
    escape the user's own directory. The payload comes from another
    process (job-hunter-api) via a shared DB row, not from this one."""
    from hunter.users import user_paths

    relpath = (relpath or "").strip()
    if not relpath or Path(relpath).is_absolute():
        raise ValueError(f"invalid or unsafe relative path: {relpath!r}")

    root = user_paths(user_id).root.resolve()
    candidate = (root / relpath).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError(f"path escapes user directory: {relpath!r}")
    return candidate


def _run_render_job(user_id: str, payload: str) -> str:
    from hunter.profile_render import render_all
    from hunter.profile_schema import from_dict, to_dict, validate
    from hunter.users import user_paths

    data = json.loads(payload)
    profile = from_dict(data)
    problems = validate(profile)
    if problems:
        # Mirrors tools/render_profile.py's own contract: a document that
        # fails validate() still renders (this drain transforms data, it
        # doesn't gate content quality — that's the site's confirmation
        # screen's job), but the gap is logged so it's not mistaken silently
        # for a clean save.
        logger.warning(
            "profile_jobs: render job for user=%s has unresolved problems: %s",
            user_id,
            "; ".join(problems),
        )

    candidate_dir = user_paths(user_id).candidate_dir
    written = render_all(profile, candidate_dir)

    profile_json_path = candidate_dir / "profile.json"
    profile_json_path.write_text(
        json.dumps(to_dict(profile), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    written.append(profile_json_path)

    return json.dumps([str(p) for p in written], ensure_ascii=False)


def _run_parse_job(user_id: str, payload: str) -> str:
    from hunter.profile_parse import extract_resume_text, parse_resume_text
    from hunter.profile_schema import to_dict
    from llm_client import call_llm

    path = _resolve_user_relative_path(user_id, payload)
    text = extract_resume_text(path)
    profile = parse_resume_text(text, llm=call_llm, source_upload_id=path.stem)
    return json.dumps(to_dict(profile), ensure_ascii=False)


def _utc_timestamp() -> str:
    """Dated subfolder name for one preview run — microsecond precision so
    two runs for the same user+track can never collide, unlike the plain
    second-resolution timestamps used elsewhere in this codebase (a preview
    can plausibly be re-requested faster than a real apply)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + "Z"


def _run_preview_job(user_id: str, payload: str) -> str:
    from hunter.profile_preview import render_preview, validate_track
    from hunter.profile_schema import from_dict
    from hunter.users import user_env, user_paths

    data = json.loads(payload)
    if not isinstance(data, dict):
        raise ValueError("preview job payload must be a JSON object")
    track = validate_track(str(data.get("track") or ""))
    profile_data = data.get("profile")
    profile = from_dict(profile_data if isinstance(profile_data, dict) else {})

    paths = user_paths(user_id)
    if not paths.candidate_yaml.is_file():
        raise ValueError(
            "no candidate.yaml found for this user — publish the profile "
            "(Editor tab -> Save/Publish) before generating a test resume"
        )

    out_dir = paths.candidate_dir / "preview" / track / _utc_timestamp()
    written = render_preview(profile, track, out_dir, extra_env=user_env(user_id))
    return json.dumps([str(p) for p in written], ensure_ascii=False)


def _process_job(row: dict) -> None:
    from hunter import profile_jobs as pj

    job_id = row["id"]
    user_id = row["user_id"]
    kind = row["kind"]
    payload = row["payload"] or ""
    try:
        if kind == KIND_RENDER:
            result = _run_render_job(user_id, payload)
        elif kind == KIND_PARSE:
            result = _run_parse_job(user_id, payload)
        elif kind == KIND_PREVIEW:
            result = _run_preview_job(user_id, payload)
        else:
            raise ValueError(f"unknown profile_jobs.kind: {kind!r}")
    except Exception as e:  # noqa: BLE001 — a job failure is terminal, not a crash
        logger.warning("profile_jobs: job %s (kind=%s) failed: %s", job_id, kind, e)
        pj.fail_profile_job(job_id, str(e))
        return
    pj.finish_profile_job(job_id, result)


def drain_once(timeout_min: int = STALE_TIMEOUT_MIN) -> int:
    """Reset stale running jobs, then process every pending job to
    completion. Returns the number of jobs processed (not counting resets).
    Synchronous and context-free so tests can call it directly."""
    from hunter import profile_jobs as pj

    reset = pj.reset_stale_profile_jobs(timeout_min)
    if reset:
        logger.warning("[profile_jobs] reset %d stale running job(s) back to pending", reset)

    processed = 0
    while True:
        row = pj.claim_next_profile_job()
        if row is None:
            break
        _process_job(row)
        processed += 1
    return processed


async def scheduled_profile_jobs_drain(context: "ContextTypes.DEFAULT_TYPE") -> None:
    import asyncio

    from hunter.best_effort import best_effort

    with best_effort("profile.jobs"):
        await asyncio.to_thread(drain_once)
