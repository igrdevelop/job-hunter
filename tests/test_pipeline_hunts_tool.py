"""docs/HUNT_DRILLDOWN_PLAN.md M2 — tools/pipeline_snapshot.py hunts_list /
hunt_detail: the hunts table and one hunt's drill-down on the /pipeline page.

The DB is the real schema (init_db + the lazy DDLs) plus
tests/fixtures/pipeline_hunts/fixture.sql under a frozen clock. Two kinds of
check: explicit assertions on the rules (state per vacancy, queue position,
user scoping, backfill exclusion, missing tables -> None), and a golden
comparison against expected_hunts.json / expected_hunt_detail.json — the
files job-hunter-api's TypeScript port is tested against. Regenerate them
with UPDATE_PIPELINE_HUNTS_FIXTURE=1 after a deliberate contract change, and
update docs/PIPELINE_SNAPSHOT_CONTRACT.md + the API copy in the same change.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import tools.pipeline_snapshot as ps
from hunter import hunt_jobs, hunt_live, hunt_runs, metrics
from hunter.db import init_db

FIXTURES = Path(__file__).parent / "fixtures" / "pipeline_hunts"
NOW = datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc)
UID = "u1"


@pytest.fixture
def hunts_db(tmp_path: Path) -> Path:
    db = tmp_path / "tracker.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    # closing(): `with connect()` only commits — an unclosed connection would
    # checkpoint the WAL whenever it is collected, changing the file's bytes
    # under test_cli_reads_read_only.
    with closing(sqlite3.connect(db)) as c, c:
        hunt_live._ensure_table(c)
        hunt_runs._ensure_table(c)
        hunt_jobs._ensure_table(c)
        metrics._ensure_tables(c)
        c.executescript((FIXTURES / "fixture.sql").read_text(encoding="utf-8"))
    return db


def _conn(db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _list(db: Path, **kw: Any) -> dict[str, Any] | None:
    with closing(_conn(db)) as conn:
        return ps.hunts_list(conn, kw.pop("user_id", UID), now=NOW, **kw)


def _detail(db: Path, hunt_id: str, user_id: str = UID) -> dict[str, Any] | None:
    with closing(_conn(db)) as conn:
        return ps.hunt_detail(conn, hunt_id, user_id, now=NOW)


def _stable(value: Any) -> Any:
    """Drop the display-only `at` strings (_local_hhmm depends on today's date)."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k != "at"}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


# ── The rules ─────────────────────────────────────────────────────────────────


def test_list_is_every_hunt_live_row_newest_first(hunts_db: Path) -> None:
    hunts = _list(hunts_db)["hunts"]
    assert [h["hunt_id"] for h in hunts] == ["h_run", "h_done", "h_retry", "h_err"]
    by_id = {h["hunt_id"]: h for h in hunts}
    assert by_id["h_run"]["status"] == "running"
    assert by_id["h_done"]["status"] == "done"
    assert by_id["h_err"]["status"] == "error"
    assert by_id["h_done"]["duration_sec"] == 90
    assert by_id["h_run"]["duration_sec"] is None
    # counts only where a hunt_runs row carries this hunt_id
    assert by_id["h_done"]["counts"]["found"] == 57
    assert by_id["h_done"]["counts"]["queued"] == 10
    assert by_id["h_run"]["counts"] is None
    assert by_id["h_retry"]["counts"] is None


def test_list_vacancy_summary_by_current_state(hunts_db: Path) -> None:
    h = {x["hunt_id"]: x for x in _list(hunts_db)["hunts"]}
    assert h["h_done"]["vacancies"] == {
        "total": 13,
        "by_state": {
            "generating": 2,
            "queued": 1,
            "ready": 1,
            "sent": 1,
            "skipped": 1,
            "declined": 1,
            "failed": 1,
            "expired": 1,
            "capped": 1,
            "no_record": 1,
            "duplicate": 2,
        },
    }
    assert h["h_run"]["vacancies"] == {"total": 0, "by_state": {}}


def test_list_limit(hunts_db: Path) -> None:
    assert [h["hunt_id"] for h in _list(hunts_db, limit=2)["hunts"]] == ["h_run", "h_done"]


def test_detail_states_and_blocks(hunts_db: Path) -> None:
    d = _detail(hunts_db, "h_done")
    assert d["per_source"] == {"justjoin": 57, "pracuj": "ERR"}
    assert d["filter_reasons"] == [("level", 30), ("location", 17)]  # most frequent first
    jobs = {j["company"]: j for j in d["jobs"]}
    assert [j["company"] for j in d["jobs"]][:3] == ["Acme", "Beta", "Gamma"]  # decision order

    acme = jobs["Acme"]
    assert acme["state"] == "queued"
    assert acme["tracker"]["queue_position"] == 2  # p0 from another hunt is ahead
    assert acme["tracker"]["wait_min"] == 119

    beta = jobs["Beta"]
    assert beta["state"] == "generating"
    live = beta["run"]["live"]
    assert live["current_stage"]["stage"] == "refine"
    assert live["refine_progress"]["round"] == 2
    assert live["refine_target"] == 95

    gamma = jobs["Gamma"]
    assert gamma["state"] == "ready"
    assert gamma["tracker"]["drive_url"] == "https://drive.test/g"
    assert gamma["run"]["run_id"] == "g3"  # the backfill row is ignored
    assert gamma["run"]["verdict_final"] == 93

    assert jobs["Delta"]["state"] == "sent"
    assert jobs["Lambda"]["state"] == "declined"  # a dash in Sent is not "ready"
    assert jobs["Mu"]["state"] == "expired"  # the nightly sweep's EXPIRED, not "declined"
    assert jobs["Nu"]["state"] == "generating"  # an open re-run beats its old FAIL row
    assert jobs["Eps"]["state"] == "skipped"
    assert jobs["Eps"]["tracker"]["skip_reason"] == "doomed:pl_onsite"
    assert jobs["Zeta"]["state"] == "failed"
    assert jobs["Eta"]["state"] == "no_record"
    assert jobs["Eta"]["tracker"] is None

    # u2's applied row for the capped vacancy stays invisible to u1
    assert jobs["Theta"]["state"] == "capped"
    assert jobs["Theta"]["tracker"] is None

    iota = jobs["Iota"]
    assert (iota["state"], iota["fate_detail"]) == ("duplicate", "tracker")
    assert iota["tracker"]["status"] == "APPLIED"  # what it duplicated, shown alongside


def test_detail_is_user_scoped(hunts_db: Path) -> None:
    jobs = {j["company"]: j for j in _detail(hunts_db, "h_done", user_id="u2")["jobs"]}
    assert jobs["Theta"]["state"] == "ready"
    assert jobs["Acme"]["tracker"] is None


def test_detail_unknown_hunt_is_none(hunts_db: Path) -> None:
    assert _detail(hunts_db, "nope") is None


def test_detail_of_a_hunt_the_live_ring_dropped(hunts_db: Path) -> None:
    with closing(sqlite3.connect(hunts_db)) as c, c:
        c.execute("DELETE FROM hunt_live WHERE hunt_id = 'h_done'")
    d = _detail(hunts_db, "h_done")
    assert d is not None
    assert d["hunt"]["status"] == "done"
    assert d["hunt"]["counts"]["found"] == 57
    assert len(d["jobs"]) == 13


def test_missing_tables_are_none_never_zero(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    assert _list(db) is None  # no hunt_live
    with closing(sqlite3.connect(db)) as c, c:
        hunt_live._ensure_table(c)
        c.execute(
            'INSERT INTO hunt_live (hunt_id, "trigger", sources, started_at, step, '
            "step_started_at) VALUES ('h1', 'scheduled', '[]', '2026-09-27T08:00:00+00:00', "
            "'fetch', '2026-09-27T08:00:00+00:00')"
        )
    row = _list(db)["hunts"][0]
    assert row["counts"] is None  # no hunt_runs
    assert row["vacancies"] is None  # no hunt_jobs
    d = _detail(db, "h1")
    assert d["jobs"] is None and d["per_source"] is None and d["vacancies"] is None


def test_cli_reads_read_only(hunts_db: Path, capsys) -> None:
    before = hunts_db.read_bytes()
    assert ps.main(["--db", str(hunts_db), "--user", UID, "--hunts", "5"]) == 0
    assert len(json.loads(capsys.readouterr().out)["hunts"]) == 4
    assert ps.main(["--db", str(hunts_db), "--user", UID, "--hunt", "h_done"]) == 0
    assert len(json.loads(capsys.readouterr().out)["jobs"]) == 13
    assert ps.main(["--db", str(hunts_db), "--hunt", "missing"]) == 1
    assert hunts_db.read_bytes() == before


# ── The contract fixtures the API port is tested against ──────────────────────


@pytest.mark.parametrize(
    ("name", "build"),
    [
        ("expected_hunts.json", lambda db: _list(db)),
        ("expected_hunt_detail.json", lambda db: _detail(db, "h_done")),
    ],
)
def test_golden_contract(hunts_db: Path, name: str, build) -> None:
    actual = _stable(json.loads(json.dumps(build(hunts_db), default=str)))
    path = FIXTURES / name
    if os.environ.get("UPDATE_PIPELINE_HUNTS_FIXTURE") == "1":
        path.write_text(json.dumps(actual, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    expected = json.loads(path.read_text(encoding="utf-8"))
    assert actual == expected


def _schema_dump(db: Path) -> str:
    """Every CREATE statement of the fixture DB, one per line group, stable order."""
    with closing(sqlite3.connect(db)) as c:
        rows = c.execute(
            "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type DESC, name"
        ).fetchall()
    return "".join(f"{r[0].strip()};\n" for r in rows)


def test_schema_sql_matches_the_bots_ddl(tmp_path: Path) -> None:
    """schema.sql is what job-hunter-api applies fixture.sql to: it must be
    the bot's real DDL (init_db + the lazy tables this read touches)."""
    db = tmp_path / "schema_only.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    with closing(sqlite3.connect(db)) as c, c:
        hunt_live._ensure_table(c)
        hunt_runs._ensure_table(c)
        hunt_jobs._ensure_table(c)
        metrics._ensure_tables(c)
    actual = _schema_dump(db)
    path = FIXTURES / "schema.sql"
    if os.environ.get("UPDATE_PIPELINE_HUNTS_FIXTURE") == "1":
        path.write_text(
            "-- Generated from the bot's DDL by tests/test_pipeline_hunts_tool.py"
            " (UPDATE_PIPELINE_HUNTS_FIXTURE=1). Never edit by hand.\n" + actual,
            encoding="utf-8",
        )
    body = "".join(
        line + "\n"
        for line in path.read_text(encoding="utf-8").splitlines()
        if not line.startswith("-- Generated")
    )
    assert body == actual


def test_no_applications_table_is_no_tracker_block(tmp_path: Path) -> None:
    """The dev tracker.db holds only the lazy tables: never crash on it."""
    db = tmp_path / "dev.db"
    with closing(sqlite3.connect(db)) as c, c:
        hunt_live._ensure_table(c)
        hunt_runs._ensure_table(c)
        hunt_jobs._ensure_table(c)
        c.execute(
            'INSERT INTO hunt_live (hunt_id, "trigger", sources, started_at, step, '
            "step_started_at, finished_at) VALUES ('h1', 'scheduled', '[]', "
            "'2026-09-27T08:00:00+00:00', 'done', '2026-09-27T08:00:00+00:00', "
            "'2026-09-27T08:01:00+00:00')"
        )
        c.execute(
            "INSERT INTO hunt_jobs (hunt_id, ts, url_norm, url, fate) VALUES "
            "('h1', '2026-09-27T08:00:00+00:00', 'ex.com/a', 'https://ex.com/a', 'card')"
        )
    job = _detail(db, "h1")["jobs"][0]
    assert (job["tracker"], job["state"]) == (None, "awaiting_decision")
    assert _list(db)["hunts"][0]["vacancies"]["total"] == 1
