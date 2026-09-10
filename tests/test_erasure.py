"""tests/test_erasure.py — hunter/erasure.py::erase_user() and the
profile_jobs kind='erase' drain path.

docs/improvement-2026-09/07-COMPLIANCE_PLAN.md risk #1, milestone M1.
docs/ERASURE_CONTRACT.md documents the job payload/result shape this module
implements.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest

from hunter import erasure
from hunter import profile_jobs as pj
from hunter.db import get_db
from hunter.erasure import ErasureReport, erase_user
from hunter.schedules import profile_jobs as sched

# The four tables the plan names explicitly. Discovery may legitimately find
# more (telegram_link_codes also carries a user_id column) — this set is a
# floor, not a ceiling.
KNOWN_USER_ID_TABLES = {"applications", "telegram_links", "user_settings", "profile_jobs"}


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture()
def db(tracker_db, monkeypatch):
    monkeypatch.setattr(erasure, "DB_PATH", tracker_db)
    monkeypatch.setattr(pj, "DB_PATH", tracker_db)
    return tracker_db


@pytest.fixture()
def users_root(tmp_path, monkeypatch):
    root = tmp_path / "users"
    monkeypatch.setattr("hunter.config.USERS_ROOT", root)
    return root


@pytest.fixture()
def logs_dir(tmp_path, monkeypatch):
    d = tmp_path / "logs"
    d.mkdir()
    monkeypatch.setattr(erasure, "_LOGS_DIR_OVERRIDE", d)
    return d


# ── DB insert helpers (direct SQL — cheaper than going through tracker.py's
#    higher-level API, which stamps a lot of fields this module doesn't
#    care about) ────────────────────────────────────────────────────────────


def _insert_application(db, *, row_id: str, user_id: str, url_norm: str = "") -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, user_id, company, url_norm) VALUES (?, ?, ?, ?)",
            (row_id, user_id, "Acme", url_norm or row_id),
        )


def _insert_user_setting(db, *, user_id: str, key: str = "hunting_enabled") -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO user_settings (user_id, key, value) VALUES (?, ?, ?)",
            (user_id, key, "true"),
        )


def _insert_telegram_link(db, *, chat_id: int, user_id: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO telegram_links (chat_id, user_id, linked_at) VALUES (?, ?, ?)",
            (chat_id, user_id, datetime.now(timezone.utc).isoformat()),
        )


def _insert_telegram_link_code(db, *, code: str, user_id: str) -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO telegram_link_codes (code, user_id, expires_at) VALUES (?, ?, ?)",
            (code, user_id, "2099-01-01T00:00:00Z"),
        )


def _insert_profile_job(db, *, job_id: str, user_id: str, kind: str = "render") -> None:
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO profile_jobs (id, user_id, kind, payload, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (
                job_id,
                user_id,
                kind,
                "{}",
                datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            ),
        )


def _count(db, table: str, user_id: str) -> int:
    with get_db(db) as conn:
        return conn.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE user_id=?",  # noqa: S608 - fixed table names
            (user_id,),
        ).fetchone()["n"]


def _seed_two_users(db) -> None:
    """u1 and u2 each get one row in every known user_id table."""
    _insert_application(db, row_id="app-u1", user_id="u1")
    _insert_application(db, row_id="app-u2", user_id="u2")
    _insert_user_setting(db, user_id="u1")
    _insert_user_setting(db, user_id="u2")
    _insert_telegram_link(db, chat_id=111, user_id="u1")
    _insert_telegram_link(db, chat_id=222, user_id="u2")
    _insert_telegram_link_code(db, code="CODE01", user_id="u1")
    _insert_telegram_link_code(db, code="CODE02", user_id="u2")
    _insert_profile_job(db, job_id="job-u1", user_id="u1")
    _insert_profile_job(db, job_id="job-u2", user_id="u2")


# ── Validation ───────────────────────────────────────────────────────────────


class TestValidateUserId:
    @pytest.mark.parametrize("bad", ["", "   ", "..", ".", "../escape", "a/b", "a\\b", "/abs"])
    def test_rejects_unsafe_ids(self, db, users_root, bad):
        with pytest.raises(ValueError):
            erase_user(bad)

    def test_accepts_a_plain_id(self, db, users_root):
        report = erase_user("plain-user-id-123")
        assert report.user_id == "plain-user-id-123"


class TestOwnerRefusal:
    def test_refuses_the_owner_without_force(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        with pytest.raises(ValueError, match="owner"):
            erase_user("owner1")

    def test_refuses_regardless_of_dry_run(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        with pytest.raises(ValueError, match="owner"):
            erase_user("owner1", dry_run=True)

    def test_force_owner_allows_it(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        _insert_application(db, row_id="app-owner", user_id="owner1")
        report = erase_user("owner1", force_owner=True)
        assert report.tables["applications"] == 1

    def test_other_users_are_unaffected_by_the_owner_check(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        _seed_two_users(db)
        # u1 is not the owner — must proceed without force_owner.
        erase_user("u1")
        assert _count(db, "applications", "u1") == 0


# ── Core erasure behavior ────────────────────────────────────────────────────


class TestEraseUser:
    def test_removes_only_the_target_users_rows_in_every_known_table(self, db, users_root):
        _seed_two_users(db)

        report = erase_user("u1")

        assert set(report.tables.keys()) >= KNOWN_USER_ID_TABLES
        for table in KNOWN_USER_ID_TABLES:
            assert _count(db, table, "u1") == 0, f"{table} still has u1 rows"
            assert _count(db, table, "u2") == 1, f"{table} lost u2's row"

    def test_report_counts_match_rows_actually_deleted(self, db, users_root):
        _insert_application(db, row_id="a1", user_id="u1")
        _insert_application(db, row_id="a2", user_id="u1")
        _insert_user_setting(db, user_id="u1")

        report = erase_user("u1")

        assert report.tables["applications"] == 2
        assert report.tables["user_settings"] == 1

    def test_dry_run_changes_nothing(self, db, users_root):
        _seed_two_users(db)

        report = erase_user("u1", dry_run=True)

        assert report.dry_run is True
        assert report.tables["applications"] == 1  # reports what WOULD be removed
        for table in KNOWN_USER_ID_TABLES:
            assert _count(db, table, "u1") == 1, f"dry run deleted from {table}"

    def test_dry_run_leaves_the_users_tree_on_disk(self, db, users_root):
        candidate_dir = users_root / "u1" / "candidate"
        candidate_dir.mkdir(parents=True)
        (candidate_dir / "candidate.yaml").write_text("x: 1\n", encoding="utf-8")

        report = erase_user("u1", dry_run=True)

        assert report.users_dir_removed is False
        assert report.files_removed == 1
        assert (candidate_dir / "candidate.yaml").exists()

    def test_removes_the_users_directory_tree(self, db, users_root):
        u1_dir = users_root / "u1"
        (u1_dir / "candidate").mkdir(parents=True)
        (u1_dir / "candidate" / "candidate.yaml").write_text("x: 1\n", encoding="utf-8")
        (u1_dir / "Applications" / "2026-01-01" / "Acme").mkdir(parents=True)
        (u1_dir / "Applications" / "2026-01-01" / "Acme" / "cv.pdf").write_bytes(b"%PDF fake")
        u2_dir = users_root / "u2"
        (u2_dir / "candidate").mkdir(parents=True)
        (u2_dir / "candidate" / "candidate.yaml").write_text("y: 2\n", encoding="utf-8")

        report = erase_user("u1")

        assert not u1_dir.exists()
        assert u2_dir.exists()
        assert report.users_dir_removed is True
        assert report.files_removed == 2
        assert report.bytes_removed > 0

    def test_no_users_directory_is_not_an_error(self, db, users_root):
        report = erase_user("nobody-here")
        assert report.users_dir_removed is False
        assert report.files_removed == 0

    def test_a_runtime_added_table_with_user_id_is_also_cleaned(self, db, users_root):
        with get_db(db) as conn:
            conn.execute(
                "CREATE TABLE extra_future_table (id TEXT PRIMARY KEY, user_id TEXT NOT NULL DEFAULT '')"
            )
            conn.execute("INSERT INTO extra_future_table (id, user_id) VALUES ('e1', 'u1')")
            conn.execute("INSERT INTO extra_future_table (id, user_id) VALUES ('e2', 'u2')")

        report = erase_user("u1")

        assert report.tables["extra_future_table"] == 1
        assert _count(db, "extra_future_table", "u1") == 0
        assert _count(db, "extra_future_table", "u2") == 1


class TestExcludeJobId:
    def test_excludes_only_the_named_job_row(self, db, users_root):
        _insert_profile_job(db, job_id="keep-me", user_id="u1")
        _insert_profile_job(db, job_id="delete-me", user_id="u1")

        report = erase_user("u1", exclude_job_id="keep-me")

        assert report.tables["profile_jobs"] == 1
        with get_db(db) as conn:
            remaining = [
                r["id"] for r in conn.execute("SELECT id FROM profile_jobs WHERE user_id='u1'")
            ]
        assert remaining == ["keep-me"]

    def test_no_exclude_id_deletes_every_row(self, db, users_root):
        _insert_profile_job(db, job_id="a", user_id="u1")
        _insert_profile_job(db, job_id="b", user_id="u1")

        erase_user("u1")

        assert _count(db, "profile_jobs", "u1") == 0


# ── Log cleanup (best-effort, name-only) ─────────────────────────────────────


class TestLogCleanup:
    def test_removes_log_files_whose_name_contains_the_uid(self, db, users_root, logs_dir):
        (logs_dir / "apply_stdout_u1_2026-01-01.log").write_text("x", encoding="utf-8")
        (logs_dir / "unrelated.log").write_text("y", encoding="utf-8")
        sub = logs_dir / "dual_shadow"
        sub.mkdir()
        (sub / "2026-01-01_u1_Acme.log").write_text("z", encoding="utf-8")

        report = erase_user("u1")

        assert not (logs_dir / "apply_stdout_u1_2026-01-01.log").exists()
        assert not (sub / "2026-01-01_u1_Acme.log").exists()
        assert (logs_dir / "unrelated.log").exists()
        assert len(report.log_files_removed) == 2

    def test_dry_run_does_not_touch_logs(self, db, users_root, logs_dir):
        target = logs_dir / "apply_stdout_u1.log"
        target.write_text("x", encoding="utf-8")

        report = erase_user("u1", dry_run=True)

        assert target.exists()
        assert report.log_files_removed == []

    def test_no_logs_directory_is_not_an_error(self, db, users_root, monkeypatch, tmp_path):
        monkeypatch.setattr(erasure, "_LOGS_DIR_OVERRIDE", tmp_path / "does-not-exist")
        report = erase_user("u1")
        assert report.log_files_removed == []


# ── tracker_cache invalidation ───────────────────────────────────────────────


class TestTrackerCacheInvalidation:
    """Uses a fresh TrackerCache() monkeypatched onto hunter.tracker_cache.cache
    for each test — mirrors tests/test_tracker_cache.py's own precedent of
    never mutating the real process-wide singleton directly, so these tests
    can't leak state into others running in the same process."""

    def _fresh_cache(self, monkeypatch):
        import hunter.tracker_cache as tracker_cache_module
        from hunter.tracker_cache import TrackerCache

        fresh = TrackerCache()
        monkeypatch.setattr(tracker_cache_module, "cache", fresh)
        return fresh

    def test_clears_the_cache_when_it_matches_the_current_process_user(
        self, db, users_root, monkeypatch
    ):
        fresh = self._fresh_cache(monkeypatch)
        monkeypatch.setenv("JOB_HUNTER_USER_ID", "u1")
        fresh.rows["r1"] = {"ID": "r1", "URL": "https://example.com/job"}
        fresh.by_url["https://example.com/job"] = "r1"
        fresh._loaded = True

        erase_user("u1")

        assert fresh.rows == {}
        assert fresh.by_url == {}
        assert fresh.loaded is False

    def test_leaves_the_cache_alone_for_a_different_user(self, db, users_root, monkeypatch):
        fresh = self._fresh_cache(monkeypatch)
        monkeypatch.setenv("JOB_HUNTER_USER_ID", "owner-process-user")
        fresh.rows["r1"] = {"ID": "r1", "URL": "https://example.com/job"}
        fresh._loaded = True

        erase_user("some-other-user")

        assert fresh.rows == {"r1": {"ID": "r1", "URL": "https://example.com/job"}}
        assert fresh.loaded is True

    def test_dry_run_never_touches_the_cache(self, db, users_root, monkeypatch):
        fresh = self._fresh_cache(monkeypatch)
        monkeypatch.setenv("JOB_HUNTER_USER_ID", "u1")
        fresh.rows["r1"] = {"ID": "r1"}
        fresh._loaded = True

        erase_user("u1", dry_run=True)

        assert fresh.rows == {"r1": {"ID": "r1"}}
        assert fresh.loaded is True


# ── to_dict() / report shape ─────────────────────────────────────────────────


def test_to_dict_matches_the_documented_contract_keys():
    report = ErasureReport(
        user_id="u1",
        dry_run=False,
        tables={"applications": 1},
        files_removed=2,
        bytes_removed=3,
        users_dir_removed=True,
        log_files_removed=["logs/x.log"],
    )
    d = report.to_dict()
    assert set(d.keys()) == {
        "user_id",
        "dry_run",
        "tables",
        "files",
        "bytes",
        "users_dir_removed",
        "log_files_removed",
    }
    assert d["files"] == 2
    assert d["bytes"] == 3


# ── profile_jobs kind='erase' round trip ─────────────────────────────────────


class TestEraseJobKind:
    def test_round_trips_through_drain_once_and_ends_done_with_report(self, db, users_root):
        _insert_application(db, row_id="a1", user_id="u1")
        job_id = str(uuid.uuid4())
        _insert_profile_job(db, job_id=job_id, user_id="u1", kind="erase")

        processed = sched.drain_once()

        assert processed == 1
        with get_db(db) as conn:
            row = dict(conn.execute("SELECT * FROM profile_jobs WHERE id=?", (job_id,)).fetchone())
        assert row["status"] == "done"
        result = json.loads(row["result"])
        assert result["tables"]["applications"] == 1
        # The job's own profile_jobs row must survive the bulk delete so
        # finish_profile_job() (called right after _run_erase_job returns)
        # has a row left to stamp 'done' onto.
        assert _count(db, "profile_jobs", "u1") == 1
        with get_db(db) as conn:
            remaining_id = conn.execute(
                "SELECT id FROM profile_jobs WHERE user_id='u1'"
            ).fetchone()["id"]
        assert remaining_id == job_id

    def test_owner_refusal_surfaces_as_a_failed_job_not_a_crash(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        job_id = str(uuid.uuid4())
        _insert_profile_job(db, job_id=job_id, user_id="owner1", kind="erase")

        processed = sched.drain_once()

        assert processed == 1
        with get_db(db) as conn:
            row = dict(conn.execute("SELECT * FROM profile_jobs WHERE id=?", (job_id,)).fetchone())
        assert row["status"] == "error"
        assert "owner" in row["error"]

    def test_force_owner_in_payload_allows_erasing_the_owner(self, db, users_root, monkeypatch):
        monkeypatch.setattr("hunter.config.DEFAULT_USER_ID", "owner1")
        _insert_application(db, row_id="a1", user_id="owner1")
        job_id = str(uuid.uuid4())
        with get_db(db) as conn:
            conn.execute(
                "INSERT INTO profile_jobs (id, user_id, kind, payload, status, created_at) "
                "VALUES (?, 'owner1', 'erase', ?, 'pending', ?)",
                (
                    job_id,
                    json.dumps({"force_owner": True}),
                    datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                ),
            )

        processed = sched.drain_once()

        assert processed == 1
        with get_db(db) as conn:
            row = dict(conn.execute("SELECT * FROM profile_jobs WHERE id=?", (job_id,)).fetchone())
        assert row["status"] == "done"

    def test_registered_in_process_job_dispatch(self):
        assert sched.KIND_ERASE == "erase"
