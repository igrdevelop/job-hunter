"""
hunter/pipeline/abort.py — post-generation abort helpers and the JobLeads
MANUAL flow for the apply pipeline. Moved out of hunter/apply_shared.py
(docs/GENERATION_ARCHITECTURE_ANALYSIS.md wave 1) — see hunter.apply_shared
for the backward-compat re-export.

``abort_after_generation()`` / ``_handle_jobleads_fetch_blocked()``
deliberately re-read ``notify`` from ``hunter.apply_shared`` (not a plain
module-global call) at call time: that module remains the attribute several
tests monkeypatch directly (test_apply_cli_abort.py, test_abort_identity.py),
and a bare in-module call would silently stop observing that patch once
these functions moved out of apply_shared.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from hunter.best_effort import best_effort
from hunter.pipeline.errors import APPLY_MANUAL_EXIT_CODE, PASTE_NO_URL_PLACEHOLDER
from hunter.pipeline.folders import _sanitize_folder_company, compute_output_folder


def abort_after_generation(
    folder: Path | None,
    url: str,
    *,
    reason: str,
    telegram_text: str = "",
    content: dict | None = None,
) -> bool:
    """Undo a package the pipeline decided to throw away AFTER it was rendered.

    The CLI pipeline's abort stages (React-only stack, company+title dedup,
    judge block, language-gate block) all run once the CLI skill has already
    rendered the documents AND written the tracker row -- `.claude/commands/
    apply.md` calls generate_docs.py WITHOUT `--no-tracker`, so the row exists
    inside the CLI call. Deleting the PDFs alone is not enough: the row stays
    APPLIED, exit 0 makes `apply_worker._resolve_outcome` see
    `has_successful_entry`, and the package is mirrored to Sheets and uploaded
    to Drive anyway (docs/STACK_PRESCREEN_PLAN.md M1 -- the 2026-08-24 Interia
    incident, and 5 more like it in the same two-week window).

    So this does all of it in one place: drop the rendered documents, settle the
    tracker row, notify. After it the parent's own terminal-row branch takes
    over: no delivery, and the URL stays deduped.

    `content` is the content.json the row was written from, and it is the
    IDENTITY -- `apply_url` and `output_folder` are the literal values
    `add_applied` stored. The pipeline's own `url` is only a fallback: paste
    mode never hands the skill a URL (the row lands with `url_norm=''`) and
    `.claude/commands/apply.md` lets the skill record the apply-button URL
    instead of the input one, so keying on it alone made this a guaranteed
    no-op for the whole paste flow.

    When no applied row could be converted, a terminal SKIP row is written here
    so no call site has to remember to. A run that ends with NO terminal row is
    not a harmless no-op: the worker clears the placeholder, the vacancy returns
    on the next hunt, the CLI regenerates the whole package and the same gate
    blocks again, forever -- the defect fixed for the backend-only pre-LLM skip
    on 2026-08-17 (one posting processed 8 times in 40 h).

    Kept on purpose: `job_posting.txt` and `content.json` (diagnostics, and the
    posting text the re-post gate reads -- a SKIP row can never become a donor).
    Only rendered output goes.

    Returns True when an applied row was converted.

    Wrapped in best_effort: the swallow is correct -- an abort must never become
    a FAIL -- but this path IS the fix for a delivery incident, so silent
    degradation has to surface as an alert. Settling NOTHING raises for exactly
    that reason: it is the failure mode with no exception of its own, and two of
    the four call sites cannot even see the return value.
    """
    from hunter.apply_shared import notify

    if folder is not None:
        for path in list(folder.glob("*.pdf")) + list(folder.glob("*.docx")):
            try:
                path.unlink()
            except OSError as e:
                print(f"[apply_agent] abort: could not delete {path.name}: {e}")

    meta = content or {}
    row_url = (meta.get("apply_url") or "").strip() or url
    row_folder = (meta.get("output_folder") or "").strip() or (
        str(folder) if folder is not None else ""
    )

    converted = False
    with best_effort("apply.abort_undo"):
        from hunter.tracker import convert_own_applied_row

        converted = convert_own_applied_row(
            row_url if row_url and row_url != PASTE_NO_URL_PLACEHOLDER else "",
            folder=row_folder,
        )
        if not converted and not _write_abort_skip_row(row_url or url, meta):
            raise RuntimeError(
                f"post-generation abort settled nothing for {row_url or url!r} "
                f"(folder={row_folder!r}) - the applied row may still be delivered"
            )

    print(
        f"[apply_agent] ABORT after generation ({reason}) -- "
        f"docs dropped, applied row converted={converted}: {row_url or url}"
    )
    if telegram_text:
        notify(telegram_text)
    return converted


def _write_abort_skip_row(url: str, content: dict) -> bool:
    """Last resort when no applied row could be converted: write the SKIP row.

    True when a row was actually written. add_skipped returns None when an
    existing terminal row already covers this URL or its company+title -- and
    that is NOT good enough here: the row it is matching may be the very applied
    row this abort failed to convert, in which case reporting success would hide
    the original incident behind a false negative.
    """
    if not url or url == PASTE_NO_URL_PLACEHOLDER:
        return False
    from hunter.models import Job
    from hunter.tracker import add_skipped

    written = add_skipped(
        Job(
            title=(content.get("job_title") or "").strip(),
            company=(content.get("company_name") or "").strip(),
            location="",
            salary=None,
            url=url,
            source="post_generation_abort",
        )
    )
    return bool(written)


# ── JobLeads MANUAL flow ──────────────────────────────────────────────────────


def _handle_jobleads_fetch_blocked(url: str, err: str, company: str = "", title: str = "") -> None:
    """Stub job_posting.txt + MANUAL tracker row; Telegram instructs user; process exits 44."""
    from hunter.apply_shared import notify
    from hunter.tracker import (
        _is_known_terminal,
        add_manual_jobleads_pending,
        has_manual_pending,
        manual_jobleads_job_posting_path,
    )
    from hunter.sources.jobleads import JOBLEADS_PASTE_MARKER

    if has_manual_pending(url):
        jp = manual_jobleads_job_posting_path(url)
        hint = f"\nFile: <code>{jp}</code>" if jp else ""
        notify(
            "📋 <b>JobLeads — MANUAL row already exists</b>\n"
            "Paste the job text into <code>job_posting.txt</code> (below the marker) and run apply "
            "again with the same URL.\n"
            f"🔗 {url}{hint}\n"
            "<i>Dedup: row already in tracker.xlsx</i>"
        )
        print(f"[apply_agent] MANUAL_PENDING (existing) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    # A PENDING/IN_PROGRESS placeholder for THIS url (M1, queue mode — the
    # worker's own claim row) must not trip this dedup check; only a genuine
    # terminal row (FAIL/SKIP/MANUAL/score/...) means "already tracked".
    if _is_known_terminal(url):
        notify(
            "📋 <b>JobLeads — URL already in tracker.xlsx</b> (dedup).\n"
            f"🔗 {url}\n"
            "If the row has status FAIL and you want MANUAL mode — delete that row in Excel and retry."
        )
        print(f"[apply_agent] MANUAL_PENDING (URL already tracked) exit={APPLY_MANUAL_EXIT_CODE}")
        sys.exit(APPLY_MANUAL_EXIT_CODE)

    company_folder = _sanitize_folder_company(company or "Unknown")
    title = (title or "Unknown").strip() or "Unknown"
    output_folder = compute_output_folder(company_folder)
    output_folder.mkdir(parents=True, exist_ok=True)

    stub = output_folder / "job_posting.txt"
    stub.write_text(
        f"URL: {url}\n\n"
        f"Company (from listing): {company or '—'}\n"
        f"Title (from listing): {title or '—'}\n\n"
        "JobLeads blocks automatic download (Cloudflare).\n"
        "Open the job in your browser, copy the full posting, and paste it below the marker line.\n\n"
        f"{JOBLEADS_PASTE_MARKER}\n\n",
        encoding="utf-8",
    )

    written = add_manual_jobleads_pending(
        url=url,
        company=company or "Unknown",
        title=title,
        folder_abs=output_folder,
    )
    folder_display = str(output_folder).replace("\\", "/")
    notify(
        "📋 <b>JobLeads — manual description required</b>\n\n"
        "Page blocked by Cloudflare. Row added to <b>tracker.xlsx</b> "
        "(ATS = <code>MANUAL</code>), folder created:\n"
        f"📁 <code>{folder_display}/</code>\n\n"
        "1. Open <code>job_posting.txt</code> in that folder\n"
        "2. Paste the full job posting <b>below</b> the marker line\n"
        "3. Save the file and run apply again <b>with the same URL</b>\n\n"
        f"🔗 {url}\n\n"
        f"<pre>{(err or '')[:280]}</pre>"
        + ("" if written else "\n\n<i>Tracker row not added (rare conflict).</i>"),
    )
    print(f"[apply_agent] MANUAL_PENDING exit={APPLY_MANUAL_EXIT_CODE} tracker_row={written}")
    sys.exit(APPLY_MANUAL_EXIT_CODE)
