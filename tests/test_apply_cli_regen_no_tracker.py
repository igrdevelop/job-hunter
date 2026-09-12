"""tests/test_apply_cli_regen_no_tracker.py — the CLI pipeline's post-processing
re-renders must never rewrite the tracker row.

`.claude/commands/apply.md` runs generate_docs.py WITHOUT --no-tracker, so by
the time `main_cli`'s post-processing starts, the documents are rendered AND
this vacancy's applied row already exists. Every re-render after that point
changes only the DOCUMENTS (cleaned content.json, NBSP-patched keywords,
a refine-loop rewrite), so each must pass no_tracker=True: in /force mode
(`skip_dedup=True`) a tracker write does DELETE+INSERT on the row, producing a
new Sheets sync ID and a false Re-application flag — the hazard CLAUDE.md
documents under "Verdict refine loop", which is why that loop already
re-renders with --no-tracker.

Source-level pinning, the repo's existing precedent for wiring guarantees —
see tests/test_prompt_injection_guard.py::
test_apply_cli_foreign_contact_regen_never_rewrites_the_tracker_row, which
pins the third of these four sites.
"""

from __future__ import annotations

import importlib
import inspect

_CALL = "build_generate_docs_cmd("


def _source_of(module_name: str) -> str:
    return inspect.getsource(importlib.import_module(module_name))


def _call_args_at(src: str, open_paren_pos: int) -> str:
    """Return the argument text of the call whose '(' sits at open_paren_pos."""
    depth = 0
    for i in range(open_paren_pos, len(src)):
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return src[open_paren_pos + 1 : i]
    raise AssertionError("unbalanced parentheses in build_generate_docs_cmd call")


def _args_of_call_before(src: str, marker: str) -> str:
    """Argument text of the last build_generate_docs_cmd call before `marker`."""
    marker_pos = src.index(marker)
    call = src.rfind(_CALL, 0, marker_pos)
    assert call != -1, f"no build_generate_docs_cmd call precedes {marker!r} anymore"
    return _call_args_at(src, call + len(_CALL) - 1)


def test_apply_cli_lang_gate_regen_never_rewrites_the_tracker_row() -> None:
    """The language enforce-gate re-renders from the cleaned content.json.

    Only the documents change, so the re-render must pass no_tracker=True —
    otherwise a /force apply DELETE+INSERTs the row the CLI skill already
    wrote (new sync ID, false Re-application flag).
    """
    src = _source_of("hunter.apply_cli")
    args = _args_of_call_before(src, "lang-gate: regenerated docs from cleaned content")
    assert "no_tracker=True" in args


def test_apply_cli_pdf_self_heal_regen_never_rewrites_the_tracker_row() -> None:
    """The NBSP self-heal re-render patches keywords and re-renders the PDF.

    Same reasoning as the language gate above: documents only, so the row the
    CLI skill wrote must be left exactly as it is.
    """
    src = _source_of("hunter.apply_cli")
    args = _args_of_call_before(src, "self-heal regen timed out")
    assert "no_tracker=True" in args


def test_every_apply_cli_regen_skips_the_tracker_write() -> None:
    """Sweep: EVERY generate_docs command built in main_cli is a re-render.

    The initial render belongs to the CLI skill, not to this module, so a new
    build_generate_docs_cmd call site here is by definition post-generation and
    must not rewrite the row. Fails loudly when a future site forgets.
    """
    src = _source_of("hunter.apply_cli")
    positions = []
    start = 0
    while (found := src.find(_CALL, start)) != -1:
        positions.append(found)
        start = found + 1
    assert len(positions) >= 4, (
        "expected the language gate, foreign-contact guard, PDF self-heal and "
        f"refine-loop re-renders — found {len(positions)} call site(s)"
    )
    for pos in positions:
        args = _call_args_at(src, pos + len(_CALL) - 1)
        assert "no_tracker=True" in args, (
            "a main_cli re-render builds generate_docs without no_tracker=True: "
            f"{args.strip()[:120]!r}"
        )
