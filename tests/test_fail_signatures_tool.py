"""Tests for tools/fail_signatures.py (docs/APPLY_FAILURE_QUEUES_PLAN.md M0)."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import tools.fail_signatures as fs
from hunter.tracker import normalize_url

T0 = datetime(2026, 9, 1, 8, 0, tzinfo=timezone.utc)
DENY = 'Permission deny rule "/apply" matches no known tool — check for typos.'
IMPORT = "ModuleNotFoundError: No module named 'hunter.missing'"


def _rec(ts: datetime, url: str, error: str, *, cli: bool = False, exit_code: int = 1) -> str:
    return json.dumps(
        {
            "ts": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "url": url,
            "company": "Co",
            "title": "Dev",
            "outcome": "fail",
            "exit_code": exit_code,
            "error": error,
            "duration_sec": 3.0,
            "cli_mode": cli,
        }
    )


def _write(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _recurring_import_failures(start: datetime, n: int, host: str = "a.example") -> list[str]:
    return [
        _rec(start + timedelta(minutes=30 * i), f"https://{host}/job/{i}", IMPORT) for i in range(n)
    ]


def _spread_singletons(n: int) -> list[str]:
    # One vacancy-class failure a day, each with its own posting → keeps the
    # window span realistic without creating a recurring signature.
    return [
        _rec(T0 + timedelta(days=i), f"https://v.example/{i}", "ERROR: posting too short")
        for i in range(n)
    ]


class TestGroupingAndPeak:
    def test_rotated_backups_are_read_oldest_first(self, tmp_path: Path) -> None:
        base = tmp_path / "apply_failures.jsonl"
        _write(base.with_name("apply_failures.jsonl.1"), [_rec(T0, "https://a.example/1", IMPORT)])
        _write(base, [_rec(T0 + timedelta(hours=1), "https://a.example/2", IMPORT)])
        files = fs.log_files(base)
        assert [p.name for p in files] == ["apply_failures.jsonl.1", "apply_failures.jsonl"]
        records, bad = fs.load_records(files)
        assert len(records) == 2 and bad == 0

    def test_bad_lines_are_counted_not_fatal(self, tmp_path: Path) -> None:
        base = tmp_path / "apply_failures.jsonl"
        _write(base, ["{not json", _rec(T0, "https://a.example/1", IMPORT), '{"no_ts": 1}'])
        records, bad = fs.load_records([base])
        assert len(records) == 1
        assert bad == 2

    def test_distinct_vacancies_and_six_hour_peak(self, tmp_path: Path) -> None:
        lines = _recurring_import_failures(T0, 6)  # 6 vacancies within 2.5h
        lines.append(_rec(T0 + timedelta(days=2), "https://a.example/job/0", IMPORT))  # repeat
        base = tmp_path / "f.jsonl"
        _write(base, lines)
        records, _ = fs.load_records([base])
        (group,) = fs.group_records(records)
        assert len(group.records) == 7
        assert len(group.distinct_urls) == 6
        assert group.peak_distinct() == 6

    def test_peak_counts_only_inside_the_window(self, tmp_path: Path) -> None:
        lines = [
            _rec(T0 + timedelta(hours=7 * i), f"https://a.example/{i}", IMPORT) for i in range(4)
        ]
        base = tmp_path / "f.jsonl"
        _write(base, lines)
        records, _ = fs.load_records([base])
        (group,) = fs.group_records(records)
        assert group.peak_distinct() == 1

    def test_since_filter(self, tmp_path: Path) -> None:
        base = tmp_path / "f.jsonl"
        _write(base, [_rec(T0, "https://a.example/1", IMPORT)])
        records, _ = fs.load_records([base], since=T0 + timedelta(seconds=1))
        assert records == []


class TestDecisionRules:
    def _evaluate(self, tmp_path: Path, lines: list[str]) -> dict:
        base = tmp_path / "f.jsonl"
        _write(base, lines)
        records, _ = fs.load_records([base])
        return fs.evaluate(fs.group_records(records), records)

    def test_recurring_non_incident_signature_means_build(self, tmp_path: Path) -> None:
        lines = _spread_singletons(10) + _recurring_import_failures(T0 + timedelta(days=3), 5)
        summary = self._evaluate(tmp_path, lines)
        assert summary["verdict"].startswith("BUILD M2+M3")
        assert len(summary["rule1_recurring"]) == 1

    def test_incident_alone_does_not_justify_m2(self, tmp_path: Path) -> None:
        incident = [
            _rec(T0 + timedelta(days=3, minutes=20 * i), f"https://x.example/{i}", DENY, cli=True)
            for i in range(40)
        ]
        summary = self._evaluate(tmp_path, _spread_singletons(10) + incident)
        assert summary["verdict"].startswith("SHIP M1 + M4 ONLY")
        # ...but it is still listed for hand-labelling under rule 2.
        assert len(summary["rule2_over_threshold"]) == 1

    def test_short_window_is_unmeasurable(self, tmp_path: Path) -> None:
        summary = self._evaluate(tmp_path, _recurring_import_failures(T0, 6))
        assert summary["verdict"].startswith("UNMEASURABLE")

    def test_source_scoped_signature_is_flagged(self, tmp_path: Path) -> None:
        lines = _spread_singletons(10) + _recurring_import_failures(
            T0 + timedelta(days=3), 5, host="himalayas.app"
        )
        summary = self._evaluate(tmp_path, lines)
        assert summary["rule3_source_scoped"] == summary["rule1_recurring"]

    def test_cross_source_signature_is_not_source_scoped(self, tmp_path: Path) -> None:
        lines = _spread_singletons(10) + [
            _rec(T0 + timedelta(days=3, minutes=10 * i), f"https://h{i}.example/j", IMPORT)
            for i in range(5)
        ]
        summary = self._evaluate(tmp_path, lines)
        assert summary["rule1_recurring"]
        assert summary["rule3_source_scoped"] == []


class TestTrackerJoin:
    @pytest.fixture
    def db(self, tmp_path: Path) -> Path:
        path = tmp_path / "tracker.db"
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE applications (url_norm TEXT, ats_status TEXT, fail_count INTEGER)"
        )
        rows = [
            (normalize_url("https://a.example/job/0"), "FAIL", 3),  # given up
            (normalize_url("https://a.example/job/1"), "FAIL", 1),  # retryable
            (normalize_url("https://a.example/job/2"), "87", 0),  # applied later
            (normalize_url("https://a.example/job/3"), "PENDING", 0),
            (normalize_url("https://a.example/job/4"), "EXPIRED", 0),
            # multi-user: a second row for job/1 that did get applied wins
            (normalize_url("https://a.example/job/1"), "91", 0),
        ]
        conn.executemany("INSERT INTO applications VALUES (?,?,?)", rows)
        conn.commit()
        conn.close()
        return path

    def test_fates(self, db: Path) -> None:
        urls = {normalize_url(f"https://a.example/job/{i}") for i in range(6)}
        states = fs.tracker_states(db, urls)
        assert states[normalize_url("https://a.example/job/0")] == "given_up"
        assert states[normalize_url("https://a.example/job/1")] == "applied"
        assert states[normalize_url("https://a.example/job/2")] == "applied"
        assert states[normalize_url("https://a.example/job/3")] == "queued"
        assert states[normalize_url("https://a.example/job/4")] == "expired"
        assert normalize_url("https://a.example/job/5") not in states

    def test_db_is_opened_read_only(self, db: Path, monkeypatch) -> None:
        before = db.read_bytes()
        fs.tracker_states(db, {normalize_url("https://a.example/job/0")})
        assert db.read_bytes() == before

    def test_old_db_without_fail_count_column(self, tmp_path: Path) -> None:
        path = tmp_path / "old.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE applications (url_norm TEXT, ats_status TEXT)")
        conn.execute("INSERT INTO applications VALUES (?, 'FAIL')", ("https://a.example/x",))
        conn.commit()
        conn.close()
        states = fs.tracker_states(path, {"https://a.example/x"})
        assert states["https://a.example/x"] == "fail_retryable"


class TestMain:
    def test_json_output_end_to_end(self, tmp_path: Path, capsys) -> None:
        base = tmp_path / "apply_failures.jsonl"
        _write(base, _spread_singletons(10) + _recurring_import_failures(T0 + timedelta(days=3), 5))
        assert fs.main(["--log", str(base), "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["summary"]["verdict"].startswith("BUILD")
        by_sig = {s["signature"]: s for s in payload["signatures"]}
        assert by_sig["ERROR: posting too short"]["distinct_vacancies"] == 10
        assert by_sig["ERROR: posting too short"]["peak_distinct_6h"] == 1
        imp = next(s for s in payload["signatures"] if s["signature"].startswith("ModuleNotFound"))
        assert imp["distinct_vacancies"] == 5
        assert imp["sig_id"] in payload["summary"]["rule1_recurring"]

    def test_text_output_with_no_log(self, tmp_path: Path, capsys) -> None:
        assert fs.main(["--log", str(tmp_path / "missing.jsonl")]) == 0
        out = capsys.readouterr().out
        assert "(none found)" in out
        assert "UNMEASURABLE" in out
