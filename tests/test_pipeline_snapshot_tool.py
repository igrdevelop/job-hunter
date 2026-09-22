"""Tests for tools/pipeline_snapshot.py (docs/PIPELINE_VIZ_PLAN.md M0 + M1 readers).

Builds a small tracker.db in tmp_path with the real schema (hunter.db.init_db
+ the lazy-ensure DDL of source_runs / postings_seen / metrics / hunt_runs),
populates every stack the page shows, and checks the snapshot's counts and
the decision rules. The fixture mixes pre-M1 and M1 shapes on purpose: the
finished runs carry end-of-stage events only (pre-M1), the in-progress run
carries `start` + refine-round events (M1), PENDING rows carry `queued_at`.
Also pins the read-only contract: the file must not change.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.pipeline_snapshot as ps
from hunter import hunt_runs, metrics, postings_seen, source_health
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
        hunt_runs._ensure_table(c)
        c.execute("CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT)")

        # hunt_runs (M1): two hunts in the window + one three days old that
        # the window must cut. Totals: found 230, filtered 190, dup 33/2/1,
        # new 4, capped 1, queued 3.
        for ts, trig, srcs, found, filt, reasons, du, dc, dcool, new, cap, q in (
            (now - timedelta(minutes=50), "scheduled", ["justjoin"], 120, 100,
             {"location": 60, "level": 40}, 15, 2, 1, 2, 0, 2),
            (now - timedelta(minutes=10), "manual", ["pracuj", "justjoin"], 110, 90,
             {"location": 50, "keyword": 40}, 18, 0, 0, 2, 1, 1),
            (now - timedelta(days=3), "scheduled", ["justjoin"], 999, 999,
             {"location": 999}, 0, 0, 0, 0, 0, 0),
        ):  # fmt: skip
            c.execute(
                'INSERT INTO hunt_runs (ts, "trigger", sources, found, filtered_out, '
                'filter_reasons, dup_url, dup_ct, dup_cooldown, "new", capped, queued, '
                "applied_inline, duration_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    _iso(ts),
                    trig,
                    json.dumps(srcs),
                    found,
                    filt,
                    json.dumps(reasons),
                    du,
                    dc,
                    dcool,
                    new,
                    cap,
                    q,
                    0,
                    5000,
                ),  # fmt: skip
            )

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

        # queued_at (M1) in the queue's own `%Y-%m-%dT%H:%M:%SZ` format
        def _q(minutes: int) -> str:
            return (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")

        app("p1", "PENDING", "Acme", queued_at=_q(45))
        app("p2", "PENDING", "Beta", queued_at=_q(20))
        app(
            "ip",
            "IN_PROGRESS",
            "Example Corp",
            claimed_at=(now - timedelta(minutes=14)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        app("a1", "94", "Gamma", ats_verdict=96, cost_usd=0.31)
        app("a2", "88", "Delta", ats_verdict=90)
        app("a4", "95", "Zeta", sent=today, ats_verdict=97, cost_usd=0.5)
        # CLI-served run: cost_usd is 0.0, not NULL — must count as unpriced
        app("a5", "92", "Omega", ats_verdict=91, cost_usd=0.0)
        # owner declined by hand (web-UI "Filter miss" writes a dash): not ready
        app("a6", "89", "Psi", sent="—", ats_verdict=80)
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
            events: list[tuple],
            pipeline: str = "cli",
        ) -> None:
            """`events`: (minutes_offset, stage, event[, payload_dict])."""
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
            for off, stage, event, *rest in events:
                payload = json.dumps(rest[0]) if rest else ""
                c.execute(
                    "INSERT INTO pipeline_events (run_id, ts, stage, event, duration_ms, payload) "
                    "VALUES (?,?,?,?,?,?)",
                    (rid, _iso(started + timedelta(minutes=off)), stage, event, 1000, payload),
                )

        std = [(0, "fetch", "ok"), (5, "generate", "ok"), (7, "judge", "ok"), (10, "verdict", "ok")]
        # The in-progress run is M1-shaped: the refine loop opened with a
        # `start` 3 min ago and has decided two rounds since.
        m1_refine = [
            (11, "refine", "start", {"target": 95, "max_rounds": 5, "verdict_first": 85}),
            (12, "refine", "rejected", {"round": 1, "kind": "honest", "score": 84, "best": 85}),
            (13, "refine", "accepted", {"round": 2, "kind": "honest", "score": 90, "best": 90}),
        ]
        run("r_ip", "ex.com/ip", now - timedelta(minutes=14), None, None, std + m1_refine)
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
        # a run the PARENT stamped after the subprocess was killed (M1 orphan
        # stamp) — closed, so not a leak; counted as orphan_stamped. Under a
        # minute so it stays out of rule 2's shares; no applications row, so
        # rule 1 is unchanged.
        run(
            "r_orph",
            "ex.com/orph",
            s,
            s + timedelta(seconds=30),
            "orphan:cli_timeout",
            [(0, "fetch", "ok")],
        )
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
    assert h["entered_tracker"]["rows"] == 12  # u2's row excluded
    assert h["entered_tracker"]["by_status"]["PENDING"] == 2


def test_hunt_runs_block_is_the_loops_own_funnel(fixture_db: Path) -> None:
    hr = _snap(fixture_db)["hunt"]["hunt_runs"]
    assert hr["hunts"] == 2  # the 3-day-old row is outside the window
    assert (hr["found"], hr["filtered_out"], hr["dup_url"], hr["dup_ct"], hr["dup_cooldown"]) == (
        230, 190, 33, 2, 1,
    )  # fmt: skip
    assert (hr["new"], hr["capped"], hr["queued"], hr["applied_inline"]) == (4, 1, 3, 0)
    assert hr["by_trigger"] == {"scheduled": 1, "manual": 1}
    # merged across hunts; ties keep first-seen order (level before keyword)
    assert hr["top_filter_reasons"] == [("location", 110), ("level", 40), ("keyword", 40)]
    assert hr["last"]["trigger"] == "manual"
    assert hr["last"]["sources"] == ["pracuj", "justjoin"]
    assert (hr["last"]["found"], hr["last"]["new"]) == (110, 2)


def test_apply_tier_queue_and_card(fixture_db: Path) -> None:
    a = _snap(fixture_db)["apply"]
    assert a["queue_mode_observed"] is True  # the IN_PROGRESS row carries claimed_at
    assert a["pending"]["count"] == 2
    assert [r["company"] for r in a["pending"]["head"]] == ["Acme", "Beta"]  # FIFO by rowid
    # queued_at (M1): the head row has waited ~45 min, the next ~20
    assert 44 <= a["pending"]["oldest_wait_min"] <= 46
    assert 44 <= a["pending"]["head"][0]["wait_min"] <= 46
    assert 19 <= a["pending"]["head"][1]["wait_min"] <= 21
    card = a["in_progress"]["cards"][0]
    assert card["company"] == "Example Corp"
    assert 13 <= card["claimed_min_ago"] <= 15
    assert card["stale"] is False
    run = card["run"]
    # M1: the run's newest event is a refine-round decision, so the stage is
    # OBSERVED (basis names the round outcome), not inferred from an `ok`.
    assert run["last_event"] == {
        "stage": "refine",
        "event": "accepted",
        "at": run["last_event"]["at"],
    }
    assert run["current_stage"] == {"stage": "refine", "basis": "refine round accepted"}
    # minutes since the refine loop's own `start` event (+11 min into a run
    # that began 14 min ago)
    assert 2 <= run["stage_started_min_ago"] <= 4
    assert run["refine_progress"] == {
        "round": 2,
        "kind": "honest",
        "score": 90,
        "best": 90,
        "outcome": "accepted",
        "at": run["refine_progress"]["at"],
    }
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
    # a1 + a2 + a5; a4 is sent, a6 is owner-declined (dash), u2's row excluded
    assert r["ready"]["count"] == 3
    assert r["ready"]["mean_verdict"] == 92.3
    assert r["sent_in_window"] == 1
    assert r["cost"] == {
        "total_usd": 0.81,
        "priced_rows": 2,
        "unpriced_rows": 3,  # a2 NULL, a5 0.0 (CLI-served), a6 NULL
        "per_priced_row_usd": 0.41,
    }


def test_events_newest_first(fixture_db: Path) -> None:
    ev = _snap(fixture_db)["events"]
    # the newest event is now the in-progress run's last refine round (M1)
    assert ev[0]["stage"] == "refine" and ev[0]["event"] == "accepted"
    assert ev[0]["company"] == "Example Corp"
    assert ev[0]["payload"].startswith('{"round": 2')
    assert [e["ts"] for e in ev] == sorted((e["ts"] for e in ev), reverse=True)


def test_coverage_rules(fixture_db: Path) -> None:
    cov = _snap(fixture_db)["coverage"]
    # 9 produced rows (a1 a2 a4 a5 a6 s1 e1 f1 f2); a5, a6 have no run and f2
    # has only a backfill run → 6/9.
    r1 = cov["1_run_coverage"]
    assert (r1["rows_produced"], r1["with_generation_run"], r1["verdict"]) == (9, 6, "FAIL")
    assert r1["excluded_blank_source"] == 0
    r2 = cov["2_stage_resolution"]
    assert r2["finished_runs_over_1min"] == 3
    assert r2["start_events_seen"] == 0
    assert r2["verdict"] == "FAIL"  # the 20-min tail after `verdict ok` dominates
    r3 = cov["3_hunt_funnel"]
    assert r3["found_raw"] == 230 and r3["unique_seen"] == 10 and r3["verdict"] == "FAIL"
    # 3b (M1): the derived gap above still fails on this fixture, but the
    # loop's own funnel exists — that is the rule the page now builds on.
    r3b = cov["3b_hunt_runs_present"]
    assert (r3b["hunts_in_window"], r3b["found"], r3b["new"], r3b["queued"]) == (2, 230, 4, 3)
    assert r3b["verdict"] == "PASS"
    r4 = cov["4_ready_stack"]
    assert (r4["ready_by_snapshot"], r4["declined_by_owner_dash"], r4["verdict"]) == (3, 1, "PASS")
    r5 = cov["5_leaked_open_runs"]
    assert (r5["open_runs"], r5["older_than_timeout"], r5["verdict"]) == (2, 1, "FAIL")
    # the parent-stamped run is closed (not a leak) and reported on its own
    assert r5["orphan_stamped"] == 1


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
    assert snap["hunt"]["hunt_runs"] is None
    assert snap["hunt"]["source_runs"] is None
    assert snap["hunt"]["postings_seen"] is None
    assert snap["apply"]["pending"]["oldest_wait_min"] is None
    assert snap["apply"]["runs"] is None
    assert snap["apply"]["queue_mode_observed"] is False
    assert snap["events"] is None
    for key in (
        "1_run_coverage",
        "2_stage_resolution",
        "3_hunt_funnel",
        "3b_hunt_runs_present",
        "5_leaked_open_runs",
    ):
        assert snap["coverage"][key]["verdict"] == "UNMEASURED", key
    assert snap["coverage"]["4_ready_stack"]["verdict"] == "PASS"


def test_null_queued_at_at_queue_head_reports_no_wait(fixture_db: Path) -> None:
    """Same rule as tracker.oldest_pending_wait_min: a legacy row (NULL
    queued_at) at the HEAD of the queue reports None — the tool must not skip
    ahead to the younger stamped row behind it."""
    with sqlite3.connect(fixture_db) as c:
        c.execute("UPDATE applications SET queued_at = NULL WHERE id = 'p1'")
    p = _snap(fixture_db)["apply"]["pending"]
    assert p["count"] == 2
    assert p["oldest_wait_min"] is None
    assert p["head"][0]["wait_min"] is None
    assert 19 <= p["head"][1]["wait_min"] <= 21


def test_pre_m1_run_card_keeps_the_inference_branch(fixture_db: Path) -> None:
    """A run with end-of-stage events only (pre-M1) still gets a card: the
    stage is inferred and the M1-only fields are None, never an error."""
    with sqlite3.connect(fixture_db) as c:
        c.execute("DELETE FROM pipeline_events WHERE run_id = 'r_ip' AND stage = 'refine'")
    run = _snap(fixture_db)["apply"]["in_progress"]["cards"][0]["run"]
    assert run["last_event"]["stage"] == "verdict"
    assert run["current_stage"] == {"stage": "refine", "basis": "inferred: after 'verdict' ok"}
    assert run["stage_started_min_ago"] is None
    assert run["refine_progress"] is None


def test_infer_stage_branches() -> None:
    class Row(dict):
        def __getitem__(self, k: str) -> object:  # sqlite3.Row-like access
            return dict.__getitem__(self, k)

    assert ps._infer_stage([])["stage"] == "fetch"
    assert ps._infer_stage([Row(stage="judge", event="start")])["stage"] == "judge"
    assert ps._infer_stage([Row(stage="judge", event="blocked")])["stage"] == "judge"
    assert ps._infer_stage([Row(stage="render", event="ok")])["stage"] == "verdict"
    assert ps._infer_stage([Row(stage="delivery", event="ok")])["stage"] == "delivery"
    # M1: a refine-round event means the loop is still running — NOT "after
    # refine", which the plain next-stage inference would say.
    for outcome in ("accepted", "rejected", "discarded"):
        got = ps._infer_stage([Row(stage="refine", event=outcome)])
        assert got == {"stage": "refine", "basis": f"refine round {outcome}"}


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
