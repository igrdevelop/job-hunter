"""Smoke tests for tools/sync_verdicts.py and tools/sync_costs.py dry-run.

Regression for a review finding: both tools imported `DB_PATH` from hunter.db,
which does not exist (`ImportError` on every --dry-run invocation; the name is
`TRACKER_DB_PATH` in hunter.config). The non-dry-run path never hit the import,
so the break was invisible until someone previewed a backfill.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dry_run(monkeypatch, tmp_path, tool_name: str) -> str:
    import hunter.config as config
    import hunter.db as db

    db_path = tmp_path / "tracker.db"
    db.init_db(db_path)
    monkeypatch.setattr(config, "TRACKER_DB_PATH", db_path)

    mod = _load_tool(tool_name)
    monkeypatch.setattr(mod, "_sheet_id", lambda: "SHEET")
    monkeypatch.setattr(sys, "argv", [f"{tool_name}.py", "--dry-run"])
    assert mod.main() == 0


def test_sync_verdicts_dry_run_smoke(monkeypatch, tmp_path, capsys) -> None:
    _dry_run(monkeypatch, tmp_path, "sync_verdicts")
    out = capsys.readouterr().out
    assert "would write 0 rows to N column" in out


def test_sync_costs_dry_run_smoke(monkeypatch, tmp_path, capsys) -> None:
    _dry_run(monkeypatch, tmp_path, "sync_costs")
    out = capsys.readouterr().out
    assert "would write 0 rows to M column" in out
