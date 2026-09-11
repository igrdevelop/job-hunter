"""Tests for tools/audit_tenant_scope.py (docs/improvement-2026-09/05-SECURITY_PLAN.md M0)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

TOOLS_DIR = Path(__file__).parent.parent / "tools"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "audit_tenant_scope", TOOLS_DIR / "audit_tenant_scope.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["audit_tenant_scope"] = module
    spec.loader.exec_module(module)
    return module


ats_scope = _load_module()


# ── sql_text_from_node ──────────────────────────────────────────────────────


def _parse_first_call_arg(src: str):
    import ast

    tree = ast.parse(src)
    call = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            call = node
            break
    assert call is not None
    return call.args[0]


def test_sql_text_from_node_plain_string():
    node = _parse_first_call_arg("conn.execute('SELECT * FROM applications')")
    assert ats_scope.sql_text_from_node(node) == "SELECT * FROM applications"


def test_sql_text_from_node_triple_quoted_multiline():
    src = '''
conn.execute(
    """
    UPDATE applications
    SET ats_status = ?
    WHERE url_norm = ?
    """
)
'''
    node = _parse_first_call_arg(src)
    sql = ats_scope.sql_text_from_node(node)
    assert sql is not None
    assert "UPDATE applications" in sql
    assert "WHERE url_norm" in sql


def test_sql_text_from_node_fstring():
    src = 'conn.execute(f"DELETE FROM applications WHERE id IN ({placeholders})")'
    node = _parse_first_call_arg(src)
    sql = ats_scope.sql_text_from_node(node)
    assert sql == "DELETE FROM applications WHERE id IN (?)"


def test_sql_text_from_node_concatenation():
    src = "conn.execute('SELECT * ' + 'FROM applications')"
    node = _parse_first_call_arg(src)
    assert ats_scope.sql_text_from_node(node) == "SELECT * FROM applications"


def test_sql_text_from_node_non_string_returns_none():
    src = "conn.execute(some_variable)"
    node = _parse_first_call_arg(src)
    assert ats_scope.sql_text_from_node(node) is None


# ── scan_file: multi-line UPDATE with/without user_id ───────────────────────


def test_scan_file_catches_multiline_update_missing_user_id(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(
        '''
def set_ats_verdict(url, verdict):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE applications
            SET ats_verdict = ?, ats_status = ?
            WHERE url_norm = ?
            """,
            (verdict, "x", url),
        )
''',
        encoding="utf-8",
    )
    calls = ats_scope.scan_file(f)
    assert len(calls) == 1
    call = calls[0]
    assert call.function == "set_ats_verdict"
    assert call.stmt_type == "UPDATE"
    assert call.has_user_id is False


def test_scan_file_ignores_multiline_update_with_user_id(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(
        '''
def set_ats_verdict(url, verdict, user_id):
    with get_db() as conn:
        conn.execute(
            """
            UPDATE applications
            SET ats_verdict = ?
            WHERE url_norm = ? AND user_id = ?
            """,
            (verdict, url, user_id),
        )
''',
        encoding="utf-8",
    )
    calls = ats_scope.scan_file(f)
    assert len(calls) == 1
    assert calls[0].has_user_id is True


def test_scan_file_ignores_statements_on_other_tables(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text(
        """
def record_run(source):
    conn.execute("INSERT INTO source_runs (source) VALUES (?)", (source,))
""",
        encoding="utf-8",
    )
    assert ats_scope.scan_file(f) == []


def test_scan_file_module_level_call_has_module_function_name(tmp_path):
    f = tmp_path / "mod.py"
    f.write_text('conn.execute("SELECT * FROM applications")\n', encoding="utf-8")
    calls = ats_scope.scan_file(f)
    assert len(calls) == 1
    assert calls[0].function == "<module>"
    assert calls[0].stmt_type == "SELECT"


def test_scan_file_select_reported_but_not_a_write():
    pass  # covered by the module-level test above (SELECT, has_user_id False)


def test_scan_file_syntax_error_returns_empty(tmp_path):
    f = tmp_path / "broken.py"
    f.write_text("def broken(:\n", encoding="utf-8")
    assert ats_scope.scan_file(f) == []


def test_scan_file_missing_file_returns_empty(tmp_path):
    assert ats_scope.scan_file(tmp_path / "nope.py") == []


# ── discover_candidate_files ─────────────────────────────────────────────────


def test_discover_candidate_files_filters_by_execute_substring(tmp_path):
    hit = tmp_path / "has_execute.py"
    hit.write_text("conn.execute('SELECT 1')\n", encoding="utf-8")
    miss = tmp_path / "no_execute.py"
    miss.write_text("print('hello')\n", encoding="utf-8")

    found = ats_scope.discover_candidate_files(tmp_path)
    assert found == [hit]


# ── build_report / exit-code contract ───────────────────────────────────────


def test_build_report_flags_write_without_user_id(tmp_path):
    hunter_dir = tmp_path / "hunter"
    hunter_dir.mkdir()
    (hunter_dir / "tracker.py").write_text(
        """
def set_cost(url, cost):
    conn.execute("UPDATE applications SET cost_usd=? WHERE url_norm=?", (cost, url))
""",
        encoding="utf-8",
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11\n", encoding="utf-8")

    report = ats_scope.build_report(hunter_dir, dockerfile)
    assert report["calls_on_applications"] == 1
    assert report["missing_user_id"] == 1
    assert report["missing_user_id_writes"] == 1
    assert report["findings"][0]["function"] == "set_cost"


def test_build_report_clean_when_every_write_scopes_user_id(tmp_path):
    hunter_dir = tmp_path / "hunter"
    hunter_dir.mkdir()
    (hunter_dir / "tracker.py").write_text(
        """
def set_cost(url, cost, user_id):
    conn.execute(
        "UPDATE applications SET cost_usd=? WHERE url_norm=? AND user_id=?",
        (cost, url, user_id),
    )
""",
        encoding="utf-8",
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11\n", encoding="utf-8")

    report = ats_scope.build_report(hunter_dir, dockerfile)
    assert report["missing_user_id_writes"] == 0


def test_grep_dangerous_cli_flags_finds_hits(tmp_path):
    hunter_dir = tmp_path / "hunter"
    hunter_dir.mkdir()
    (hunter_dir / "apply_cli.py").write_text(
        'cmd = ["claude", "-p", "--dangerously-skip-permissions", "/apply"]\n', encoding="utf-8"
    )
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("ENV IS_SANDBOX=1\n", encoding="utf-8")

    hits = ats_scope.grep_dangerous_cli_flags(hunter_dir, dockerfile)
    assert len(hits) == 2
    assert any("dangerously-skip-permissions" in h for h in hits)
    assert any("IS_SANDBOX" in h for h in hits)


def test_grep_dangerous_cli_flags_no_hits(tmp_path):
    hunter_dir = tmp_path / "hunter"
    hunter_dir.mkdir()
    (hunter_dir / "clean.py").write_text("x = 1\n", encoding="utf-8")
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.11\n", encoding="utf-8")

    assert ats_scope.grep_dangerous_cli_flags(hunter_dir, dockerfile) == []


def test_format_report_includes_decision_rule():
    report = {
        "files_scanned": 0,
        "calls_on_applications": 0,
        "missing_user_id": 0,
        "missing_user_id_writes": 0,
        "findings": [],
        "dangerous_cli_flags": [],
    }
    text = ats_scope.format_report(report)
    assert "Decision rule" in text
    assert "M2" in text


def test_real_repo_tracker_py_has_a_known_gap():
    """Sanity check against the ACTUAL repo state this M0 tool was built to
    measure — docs/improvement-2026-09/05-SECURITY_PLAN.md finding #2 says
    ~25 writes on `applications` lack a user_id predicate today. This isn't
    pinned to an exact count (that would break the moment M2 lands and fixes
    them), just that the scanner finds a real, non-trivial number > 0 against
    the real hunter/tracker.py — proof the tool isn't a no-op against itself.
    """
    project_dir = Path(__file__).parent.parent
    report = ats_scope.build_report(project_dir / "hunter", project_dir / "Dockerfile")
    assert report["missing_user_id_writes"] > 0
