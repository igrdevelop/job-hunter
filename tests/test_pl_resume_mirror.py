"""Tests for the "a Polish posting must ship a Polish CV" safety net.

Live-corpus finding (2026-08-22, 761 applications on the deploy host): 15 of
250 PL applications shipped an English CV next to a Polish cover letter, all
from July onwards as production moved onto the CLI path. Two compounding
causes, one test class each:

* `.claude/commands/apply.md` told the CLI skill to return `"resume_pl": null`
  unless `--full` — unconditionally, Polish postings included;
* `apply_cli` stamped `primary_lang` only as a side effect of a repair, and
  that key is what gates BOTH generate_docs' PL-CV routing and the
  verdict-refine PL mirror, so the fallback was disabled exactly when needed.
"""

from __future__ import annotations

import pathlib
import re

from hunter import apply_shared
from hunter.apply_shared import ensure_pl_resume

REPO = pathlib.Path(__file__).resolve().parent.parent


def _content(resume_pl=None):
    return {
        "resume_en": {
            "summary": "Senior Frontend Developer.",
            "skills": {"frontend": "Angular"},
            "experience": [{"company": "Acme", "bullets": ["Built things."]}],
            "education": "BSTU",
        },
        "resume_pl": resume_pl,
    }


class TestEnsurePlResume:
    def test_mirrors_when_the_generator_returned_none(self, monkeypatch):
        seen = {}

        def fake_translate(source, target_lang, *, expected_roles):
            seen["target"] = target_lang
            seen["roles"] = expected_roles
            return {"summary": "Starszy programista.", "experience": [{"company": "Acme"}]}

        monkeypatch.setattr(apply_shared, "_translate_resume", fake_translate)
        content = _content(None)
        fixes = ensure_pl_resume(content, "PL")

        assert fixes and "mirrored resume_pl" in fixes[0]
        assert content["resume_pl"]["summary"] == "Starszy programista."
        assert seen == {"target": "PL", "roles": 1}

    def test_noop_for_an_english_posting(self, monkeypatch):
        monkeypatch.setattr(
            apply_shared,
            "_translate_resume",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not translate")),
        )
        content = _content(None)
        assert ensure_pl_resume(content, "EN") == []
        assert content["resume_pl"] is None

    def test_noop_when_a_pl_resume_is_already_there(self, monkeypatch):
        monkeypatch.setattr(
            apply_shared,
            "_translate_resume",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not translate")),
        )
        content = _content({"summary": "Już jest."})
        assert ensure_pl_resume(content, "PL") == []
        assert content["resume_pl"] == {"summary": "Już jest."}

    def test_empty_dict_counts_as_missing(self, monkeypatch):
        monkeypatch.setattr(apply_shared, "_translate_resume", lambda *a, **k: {"summary": "PL"})
        content = _content({})
        assert ensure_pl_resume(content, "PL")
        assert content["resume_pl"] == {"summary": "PL"}

    def test_translation_failure_never_raises(self, monkeypatch, tmp_path):
        # best_effort counts the failure in SQLite — point it at a temp DB so the
        # test never writes into the repo-local tracker.db.
        from hunter import best_effort as be

        monkeypatch.setattr(be, "DB_PATH", tmp_path / "tracker.db")
        monkeypatch.setattr(be, "_default_notify", lambda *a, **k: None)
        monkeypatch.setattr(
            apply_shared,
            "_translate_resume",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")),
        )
        content = _content(None)
        assert ensure_pl_resume(content, "PL") == []
        assert content["resume_pl"] is None

    def test_failure_is_counted_for_alerting(self, monkeypatch, tmp_path):
        """A mirror that silently stops working recreates the very bug this
        closes, so the failure must reach best_effort's consecutive counter."""
        from hunter import best_effort as be

        monkeypatch.setattr(be, "DB_PATH", tmp_path / "tracker.db")
        alerts: list[str] = []
        monkeypatch.setattr(be, "_default_notify", lambda msg: alerts.append(msg))
        monkeypatch.setattr(
            apply_shared,
            "_translate_resume",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("api down")),
        )
        for _ in range(3):  # default threshold
            assert ensure_pl_resume(_content(None), "PL") == []
        assert alerts, "3 consecutive PL-mirror failures must raise one alert"
        assert "apply.pl_mirror" in alerts[0]

    def test_translation_returning_none_is_not_persisted(self, monkeypatch):
        monkeypatch.setattr(apply_shared, "_translate_resume", lambda *a, **k: None)
        content = _content(None)
        assert ensure_pl_resume(content, "PL") == []
        assert content["resume_pl"] is None

    def test_noop_without_an_english_resume(self, monkeypatch):
        monkeypatch.setattr(
            apply_shared,
            "_translate_resume",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not translate")),
        )
        assert ensure_pl_resume({"resume_en": {}}, "PL") == []


class TestPipelineWiring:
    """Source-level guards (same convention as tests/test_apply_api.py) — these
    invariants live in control flow that a unit test cannot reach without
    running a whole apply."""

    def test_cli_stamps_primary_lang_outside_the_repair_branch(self):
        src = (REPO / "hunter" / "apply_cli.py").read_text(encoding="utf-8")
        stamp = src.index('_cli_content["primary_lang"] = _posting_lang')
        branch = src.index("if _report or _scrub_fixes or _pl_fixes or _pl_cv_due:")
        assert stamp < branch, "primary_lang must be stamped before (not inside) the repair branch"

    def test_cli_persists_content_json_unconditionally(self):
        src = (REPO / "hunter" / "apply_cli.py").read_text(encoding="utf-8")
        stamp = src.index('_cli_content["primary_lang"] = _posting_lang')
        branch = src.index("if _report or _scrub_fixes or _pl_fixes or _pl_cv_due:")
        between = src[stamp:branch]
        assert "content_json_path.write_text(" in between

    def test_both_pipelines_call_the_pl_net(self):
        for mod in ("apply_cli.py", "apply_api.py"):
            src = (REPO / "hunter" / mod).read_text(encoding="utf-8")
            assert "ensure_pl_resume(" in src, f"{mod} must run the PL mirror net"

    def test_cli_skill_prompt_excepts_polish_postings(self):
        md = (REPO / ".claude" / "commands" / "apply.md").read_text(encoding="utf-8")
        rule = md[md.index('set `"resume_pl": null`') :][:800]
        assert re.search(r"posting itself is written in Polish", rule), (
            "apply.md must not tell the CLI skill to null resume_pl on a Polish posting"
        )
