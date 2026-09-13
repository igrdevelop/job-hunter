"""docs/MARKET_MEMORY_PLAN.md M2 — every SKIP row carries WHY in `skip_reason`.

One assertion per writer (Skip button, doomed gate HARD, prescreen skip,
react, company+title dedup, backend-only, post-generation abort), the
normalize_skip_reason table, both in-place conversions (FAIL → SKIP and
APPLIED → SKIP) and the migration onto a pre-M2 database. The column is
reports-only: nothing here (or anywhere) gates on it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from hunter import tracker
from hunter.db import get_db, init_db
from hunter.models import Job
from hunter.tracker import (
    SKIP_REASON_DETAIL_MAX,
    SKIP_REASON_PREFIXES,
    add_react_skipped,
    add_skipped,
    convert_own_applied_row,
    normalize_skip_reason,
)


def _job(url: str, company: str = "Acme", title: str = "Senior Angular Developer") -> Job:
    return Job(title=title, company=company, location="Remote", salary=None, url=url, source="t")


def _skip_reason(url: str) -> str | None:
    with get_db(tracker.DB_PATH) as conn:
        row = conn.execute(
            "SELECT skip_reason FROM applications WHERE url_norm=?", (tracker.normalize_url(url),)
        ).fetchone()
    return row["skip_reason"] if row else None


# ── normalize_skip_reason ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", ""),
        ("   ", ""),
        ("button", "button"),
        ("doomed:foreign_onsite_hybrid", "doomed:foreign_onsite_hybrid"),
        (
            "abort:company+title dedup (acme|senior angular)",
            "abort:company+title dedup (acme|senior angular)",
        ),
        ("Doomed:rule", "doomed:rule"),
        ("REACT", "react"),
        ("doomed:rule:extra", "doomed:rule:extra"),
        ("doomed:", "doomed"),
        ("mystery", "other:mystery"),
        ("mystery:with detail", "other:mystery:with detail"),
    ],
)
def test_normalize_skip_reason_table(raw: str, expected: str) -> None:
    assert normalize_skip_reason(raw) == expected


def test_normalize_truncates_an_over_long_detail() -> None:
    out = normalize_skip_reason("abort:" + "x" * 200)
    assert out == "abort:" + "x" * SKIP_REASON_DETAIL_MAX


def test_normalize_truncates_an_unknown_prefix_too() -> None:
    out = normalize_skip_reason("y" * 200)
    assert out == "other:" + "y" * SKIP_REASON_DETAIL_MAX


def test_every_prefix_round_trips() -> None:
    for prefix in SKIP_REASON_PREFIXES:
        assert normalize_skip_reason(prefix) == prefix
        assert normalize_skip_reason(f"{prefix}:d") == f"{prefix}:d"


# ── the tracker writers ─────────────────────────────────────────────────────


class TestTrackerWriters:
    def test_add_skipped_default_is_empty(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/untagged"
        add_skipped(_job(url))
        assert _skip_reason(url) == ""

    def test_add_skipped_stamps_the_reason(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/button"
        row = add_skipped(_job(url), reason="button")
        assert _skip_reason(url) == "button"
        assert row is not None and "skip_reason" not in row, (
            "the Sheets-shaped dict must not grow a key — the column is not mirrored"
        )

    def test_add_skipped_never_fails_over_a_label(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/weird"
        add_skipped(_job(url), reason="Not A Prefix")
        assert _skip_reason(url) == "other:Not A Prefix"

    def test_add_react_skipped_defaults_to_react(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/react"
        add_react_skipped({"stack": "React", "company_name": "Acme", "job_title": "FE"}, url)
        assert _skip_reason(url) == "react"

    def test_add_react_skipped_accepts_a_more_specific_reason(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/prescreen"
        add_react_skipped(
            {"stack": "React", "company_name": "", "job_title": ""}, url, reason="prescreen"
        )
        assert _skip_reason(url) == "prescreen"

    def test_fail_row_converted_by_add_skipped_carries_the_reason(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/failed-then-doomed"
        tracker.add_failed(_job(url))
        assert add_skipped(_job(url), reason="doomed:non_eu_authorization") is None, (
            "an existing FAIL row is converted in place, not duplicated"
        )
        with get_db(tracker.DB_PATH) as conn:
            rows = conn.execute(
                "SELECT ats_status, sent, skip_reason FROM applications WHERE url_norm=?",
                (tracker.normalize_url(url),),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0]["ats_status"] == "SKIP"
        assert rows[0]["sent"] == "—"
        assert rows[0]["skip_reason"] == "doomed:non_eu_authorization"

    def test_fail_row_conversion_without_a_reason_leaves_the_column_alone(
        self, tracker_db: Path
    ) -> None:
        url = "https://example.com/jobs/failed-then-skipped"
        tracker.add_failed(_job(url))
        assert tracker._convert_own_fail_row(url, sent="—") is True
        assert _skip_reason(url) == ""

    def test_convert_own_applied_row_stamps_the_reason(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/applied-then-aborted"
        tracker.add_applied(
            {
                "company_name": "Acme",
                "job_title": "FE",
                "apply_url": url,
                "stack": "React",
                "ats_score": "90",
                "output_folder": "/tmp/acme",
            }
        )
        assert convert_own_applied_row(url, skip_reason="abort:react-only stack") is True
        assert _skip_reason(url) == "abort:react-only stack"
        assert tracker.lookup_url(url)[0]["ats"].strip().upper() == "SKIP"

    def test_convert_own_applied_row_without_a_reason_is_unchanged(self, tracker_db: Path) -> None:
        url = "https://example.com/jobs/applied-then-aborted-2"
        tracker.add_applied(
            {
                "company_name": "Acme",
                "job_title": "FE",
                "apply_url": url,
                "stack": "React",
                "ats_score": "90",
                "output_folder": "/tmp/acme2",
            }
        )
        assert convert_own_applied_row(url) is True
        assert _skip_reason(url) == ""


# ── the call sites ──────────────────────────────────────────────────────────


class TestCallSites:
    def test_skip_button_writes_button(self, tracker_db: Path) -> None:
        import asyncio

        from hunter.commands.url_message import _handle_skip

        url = "https://example.com/jobs/skip-button"

        class _Query:
            class message:
                text = "card"

            async def edit_message_text(self, *_a, **_k) -> None:
                return None

        async def _noop(*_a, **_k) -> None:
            return None

        async def _run() -> None:
            with (
                patch("hunter.tracker_cache.cache.add", _noop),
                patch("hunter.gsheets_sync.mirror_new_row", _noop),
            ):
                await _handle_skip(_Query(), _job(url), "job-1")

        asyncio.run(_run())
        assert _skip_reason(url) == "button"

    def test_doomed_gate_hard_writes_the_rule(self, tracker_db: Path) -> None:
        from hunter.filters import GateFinding
        from hunter.pipeline.gates import run_doomed_gate

        url = "https://example.com/jobs/doomed"
        finding = GateFinding(
            severity="hard", rule="foreign_onsite_hybrid", evidence="on-site in Austin, TX"
        )
        with (
            patch("hunter.config.DOOMED_GATE_ENABLED", True),
            patch("hunter.config.DOOMED_GATE_HARD_ACTION", "skip"),
            patch("hunter.filters.assess_job_text", return_value=[finding]),
            patch("hunter.apply_shared.notify"),
        ):
            assert run_doomed_gate("job text", url, title="FE", company="Acme") is True
        assert _skip_reason(url) == "doomed:foreign_onsite_hybrid"

    def test_abort_after_generation_converts_with_abort_prefix(
        self, tracker_db: Path, tmp_path: Path
    ) -> None:
        from hunter.pipeline.abort import abort_after_generation

        url = "https://example.com/jobs/abort-convert"
        folder = tmp_path / "Acme"
        folder.mkdir()
        tracker.add_applied(
            {
                "company_name": "Acme",
                "job_title": "FE",
                "apply_url": url,
                "stack": "React",
                "ats_score": "90",
                "output_folder": str(folder),
            }
        )
        with patch("hunter.apply_shared.notify"):
            assert abort_after_generation(folder, url, reason="react-only stack") is True
        assert _skip_reason(url) == "abort:react-only stack"

    def test_abort_after_generation_fallback_skip_row_carries_the_reason(
        self, tracker_db: Path, tmp_path: Path
    ) -> None:
        from hunter.pipeline.abort import abort_after_generation

        url = "https://example.com/jobs/abort-fallback"
        folder = tmp_path / "Acme"
        folder.mkdir()
        with patch("hunter.apply_shared.notify"):
            assert (
                abort_after_generation(
                    folder, url, reason="company+title dedup (acme|fe)", content={}
                )
                is False
            )
        assert _skip_reason(url) == "abort:company+title dedup (acme|fe)"


# ── migration ───────────────────────────────────────────────────────────────


def test_migration_adds_skip_reason_to_a_pre_m2_db(tmp_path: Path) -> None:
    """A tracker.db created before M2 gains the column on the next init_db."""
    import sqlite3

    db = tmp_path / "tracker.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE applications (id TEXT PRIMARY KEY, date TEXT, company TEXT, title TEXT, "
        "stack TEXT, ats_status TEXT, url TEXT, url_norm TEXT, folder TEXT, sent TEXT, "
        "reapplication TEXT, to_learn TEXT, drive_url TEXT, confirmation TEXT, answer TEXT)"
    )
    conn.execute(
        "INSERT INTO applications (id, company, title, ats_status, url, url_norm, sent) "
        "VALUES ('abcd1234', 'Old', 'Row', 'SKIP', 'https://x/1', 'https://x/1', '—')"
    )
    conn.commit()
    conn.close()

    init_db(db, xlsx_path=tmp_path / "no_tracker.xlsx")

    with get_db(db) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(applications)")}
        assert "skip_reason" in cols
        old = conn.execute("SELECT skip_reason FROM applications WHERE id='abcd1234'").fetchone()
    assert old["skip_reason"] == "", "a pre-M2 row reads as untagged, never NULL"
