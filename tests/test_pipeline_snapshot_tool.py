"""Tests for tools/pipeline_snapshot.py (docs/PIPELINE_VIZ_PLAN.md M0).

Builds a small tracker.db in tmp_path with the real schema (hunter.db.init_db
+ the lazy-ensure DDL of source_runs / postings_seen / metrics), populates
every stack the page shows, and checks the snapshot's counts and the five
decision rules. Also pins the read-only contract: the file must not change.
"""

from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.pipeline_snapshot as ps
from hunter import metrics, postings_seen, source_health
from hunter.db import init_db

UID = "u1"


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    db = tmp_path / "tracker.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    now = datetime.now(timezone.utc)
    today = date.today().strftime("%Y-%m-%d")

    with sqlite3.connect(db) as c:
        postings_seen._ensure_table(c)
        source_health._ensure_table(c)
        metrics._ensure_tables(c)
        c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")

        for src, y, ok in (("justjoin", 120, 1), ("pracuj", 0, 0), ("justjoin", 110, 1)):
            c.execute(
                "INSERT INTO source_runs (source, ts, yield, ok, error) VALUES (?,?,?,?,?)",
                (src, _iso(now - timedelta(hours=2)), y, ok, "" if ok else "429"),
            )
        for i in range(10):
            verdict = "passed" if i < 3 else "location: Berlin"
            c.execute(
                "INSERT INTO postings_seen (url_norm, url, source, first_seen, last_seen, "
                "seen_count, filter_verdict, filter_verdict_last) VALUES (?,?,?,?,?,?,?,?)",
                (
                    f"ex.com/{i}",
                    f"https://ex.com/{i}",
                    "justjoin",
                    _iso(now),
                    _iso(now),
                    1,
                    verdict,
                    verdict,
                ),
            )

        def app(id_: str, status: str, company: str, **kw: object) -> None:
            cols: dict[str, object] = {
                "id": id_,
                "date": kw.pop("date", today),
                "user_id": kw.pop("user_id", UID),
                "company": company,
                "title": "Angular Dev",
                "ats_status": status,
                "url": f"https://ex.com/{id_}",
                "url_norm": f"ex.com/{id_}",
                "sent": kw.pop("sent", ""),
                "source": kw.pop("source", "justjoin"),
                **kw,
            }
            c.execute(
                f"INSERT INTO applications ({','.join(cols)}) "  # noqa: S608 — test fixture
                f"VALUES ({','.join('?' for _ in cols)})",
                tuple(cols.values()),
            )

        app("p1", "PENDING", "Acme")
        app("p2", "PENDING", "Beta")
        app(
            "ip",
            "IN_PROGRESS",
            "Example Corp",
            claimed_at=(now - timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        app("a1", "94", "Gamma", ats_verdict=96, cost_usd=0.31)
        app("a2", "88", "Delta", ats_verdict=90)
        app("a4", "95", "Zeta", sent=today, ats_verdict=97, cost_usd=0.5)
        app("s1", "SKIP", "Theta", sent="—", skip_reason="doomed:pl_onsite")
        app("e1", "EXPIRED", "Kappa", sent="EXPIRED")
        app("f1", "FAIL", "Lambda", sent="—", fail_count=1)
        app("f2", "FAIL", "Mu", sent="—", fail_count=3)
        # another user's ready row must never leak into u1's stacks
        app("x1", "90", "Other", user_id="u2", ats_verdict=50)

        def run(
            rid: str,
            url_norm: str,
            started: datetime,
            finished: datetime | None,
            outcome: str | None,
            events: list[tuple[int, str, str]],
            pipeline: str = "cli",
        ) -> None:
            c.execute(
                "INSERT INTO generation_runs (run_id, user_id, url_norm, started_at, finished_at, "
                "pipeline, outcome, verdict_first, verdict_final, refine_rounds) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    UID,
                    url_norm,
                    _iso(started),
                    _iso(finished) if finished else None,
                    pipeline,
                    outcome,
                    85,
                    88,
                    1,
                ),
            )
            for off, stage, event in events:
                c.execute(
                    "INSERT INTO pipeline_events (run_id, ts, stage, event, duration_ms, payload) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid, _iso(started + timedelta(minutes=off)), stage, event, 1000, ""),
                )

        std = [(0, "fetch", "ok"), (5, "generate", "ok"), (7, "judge", "ok"), (10, "verdict", "ok")]
        run("r_ip", "ex.com/ip", now - timedelta(minutes=14), None, None, std)
        for rid, un in (("r_a1", "ex.com/a1"), ("r_a2", "ex.com/a2"), ("r_a4", "ex.com/a4")):
            s = now - timedelta(minutes=200)
            run(rid, un, s, s + timedelta(minutes=30), "ok", std)
        s = now - timedelta(minutes=100)
        run(
            "r_s1",
            "ex.com/s1",
            s,
            s + timedelta(seconds=20),
            "skip_doomed_gate",
            [(0, "fetch", "ok")],
        )
        run("r_e1", "ex.com/e1", s, s + timedelta(seconds=20), "expired", [(0, "fetch", "ok")])
        run("r_f1", "ex.com/f1", s, s + timedelta(seconds=20), "cli_error", [(0, "fetch", "ok")])
        # a backfilled row for f2 must NOT count as run coverage
        run("bf_f2", "ex.com/f2", s, s, "ok", [], pipeline="backfill")
        # a leaked open run, older than the CLI timeout
        run("r_leak", "ex.com/leak", now - timedelta(hours=5), None, None, [(0, "fetch", "ok")])
        c.execute(
            "INSERT INTO config (key, value) VALUES ('llm_outage_until', ?)",
            (str(int(now.timestamp()) + 1800),),
        )
    return db


def _snap(db: Path, **kw: object) -> dict:
    args = {"days": 1, "user_id": UID, "failures_log": db.parent / "nope.jsonl", "events_limit": 10}
    args.update(kw)
    return ps.build_snapshot(db, **args)  # type: ignore[arg-type]


def test_hunt_tier_counts(fixture_db: Path) -> None:
    h = _snap(fixture_db)["hunt"]
    assert h["source_runs"]["found_raw"] == 230
    assert h["source_runs"]["runs"] == 3
    assert h["source_runs"]["sources_ran"] == 2
    assert h["source_runs"]["errors"] == 1
    assert h["postings_seen"] == {
        "unique_seen": 10,
        "new_this_window": 10,
        "passed": 3,
        "rejected": 7,
        "top_reasons": [("location", 7)],
    }
    assert h["entered_tracker"]["rows"] == 10  # u2's row excluded
    assert h["entered_tracker"]["by_status"]["PENDING"] == 2


def test_apply_tier_queue_and_card(fixture_db: Path) -> None:
    a = _snap(fixture_db)["apply"]
    assert a["pending"]["count"] == 2
    assert [r["company"] for r in a["pending"]["head"]] == ["Acme", "Beta"]  # FIFO by rowid
    card = a["in_progress"]["cards"][0]
    assert card["company"] == "Example Corp"
    assert 13 <= card["claimed_min_ago"] <= 15
    assert card["stale"] is False
    run = card["run"]
    assert run["last_event"]["stage"] == "verdict"
    # No start events exist today, so the stage after the last `ok` is inferred.
    assert run["current_stage"]["stage"] == "refine"
    assert "inferred" in run["current_stage"]["basis"]
    assert a["runs"]["cut_zero_cost"] == {"expired": 1, "skip_doomed_gate": 1}
    assert a["skipped_rows"]["by_reason"] == [("EXPIRED", 1), ("doomed", 1)]
    assert a["failures"]["in_window"] == 2
    assert a["failures"]["retryable_total"] == 1
    assert a["failures"]["gave_up_total"] == 1
    assert a["failures"]["log_records"] is None  # no jsonl on disk
    assert a["llm_outage"]["paused"] is True
    assert 28 <= a["llm_outage"]["remaining_min"] <= 30


def test_result_tier(fixture_db: Path) -> None:
    r = _snap(fixture_db)["result"]
    assert r["ready"]["count"] == 2  # a1 + a2; a4 is sent; u2's row excluded
    assert r["ready"]["mean_verdict"] == 93.0
    assert r["sent_in_window"] == 1
    assert r["cost"] == {
        "total_usd": 0.81,
        "priced_rows": 2,
        "unpriced_rows": 1,
        "per_priced_row_usd": 0.41,
    }


def test_events_newest_first(fixture_db: Path) -> None:
    ev = _snap(fixture_db)["events"]
    assert ev[0]["stage"] == "verdict" and ev[0]["company"] == "Example Corp"
    assert [e["ts"] for e in ev] == sorted((e["ts"] for e in ev), reverse=True)


def test_coverage_rules(fixture_db: Path) -> None:
    cov = _snap(fixture_db)["coverage"]
    # 7 produced rows (a1 a2 a4 s1 e1 f1 f2); f2 has only a backfill run → 6/7.
    r1 = cov["1_run_coverage"]
    assert (r1["rows_produced"], r1["with_generation_run"], r1["verdict"]) == (7, 6, "FAIL")
    r2 = cov["2_stage_resolution"]
    assert r2["finished_runs_over_1min"] == 3
    assert r2["start_events_seen"] == 0
    assert r2["verdict"] == "FAIL"  # the 20-min tail after `verdict ok` dominates
    r3 = cov["3_hunt_funnel"]
    assert r3["found_raw"] == 230 and r3["unique_seen"] == 10 and r3["verdict"] == "FAIL"
    assert cov["4_ready_stack"]["verdict"] == "PASS"
    r5 = cov["5_leaked_open_runs"]
    assert (r5["open_runs"], r5["older_than_timeout"], r5["verdict"]) == (2, 1, "FAIL")


def test_read_only(fixture_db: Path) -> None:
    # init_db() puts the DB in WAL mode, so a -wal sidecar can exist from the
    # fixture itself; the contract is that the snapshot changes NO bytes of
    # the main file or the sidecar, and refuses a write on its connection.
    sidecar = fixture_db.with_name(fixture_db.name + "-wal")
    before = fixture_db.read_bytes()
    before_wal = sidecar.read_bytes() if sidecar.exists() else b""
    _snap(fixture_db)
    assert fixture_db.read_bytes() == before
    assert (sidecar.read_bytes() if sidecar.exists() else b"") == before_wal
    ro = sqlite3.connect(f"file:{fixture_db.resolve().as_posix()}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("DELETE FROM applications")
    ro.close()


def test_unmeasured_on_bare_db(tmp_path: Path) -> None:
    db = tmp_path / "bare.db"
    init_db(db, xlsx_path=tmp_path / "none.xlsx")
    snap = _snap(db)
    assert snap["hunt"]["source_runs"] is None
    assert snap["hunt"]["postings_seen"] is None
    assert snap["apply"]["runs"] is None
    assert snap["events"] is None
    for key in ("1_run_coverage", "2_stage_resolution", "3_hunt_funnel", "5_leaked_open_runs"):
        assert snap["coverage"][key]["verdict"] == "UNMEASURED", key
    assert snap["coverage"]["4_ready_stack"]["verdict"] == "PASS"


def test_infer_stage_branches() -> None:
    class Row(dict):
        def __getitem__(self, k: str) -> object:  # sqlite3.Row-like access
            return dict.__getitem__(self, k)

    assert ps._infer_stage([])["stage"] == "fetch"
    assert ps._infer_stage([Row(stage="judge", event="start")])["stage"] == "judge"
    assert ps._infer_stage([Row(stage="judge", event="blocked")])["stage"] == "judge"
    assert ps._infer_stage([Row(stage="render", event="ok")])["stage"] == "verdict"
    assert ps._infer_stage([Row(stage="delivery", event="ok")])["stage"] == "delivery"


def test_parse_ts_shapes() -> None:
    a = ps._parse_ts("2026-09-22T10:00:00+00:00")
    b = ps._parse_ts("2026-09-22T10:00:00Z")
    c = ps._parse_ts("2026-09-22T10:00:00")
    assert a == b == c
    assert ps._parse_ts("garbage") is None
    assert ps._parse_ts(None) is None


def test_main_rejects_missing_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert ps.main(["--db", str(tmp_path / "missing.db")]) == 1
    assert "not found" in capsys.readouterr().err
