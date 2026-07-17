"""
Tests for tools/dedup_drive_folders.py against an in-memory fake Drive.

Covers the full sweep: duplicate date folders under the root, duplicate
company folders inside a date folder, and — the reason this file exists —
duplicate "Logs" folders (gdrive_sync.upload_log_file's target). On Drive
the historical duplicates are all literally named "Logs"; the "Logs (1)" …
"Logs (7)" the owner sees are Drive-for-Desktop's rendering of same-named
siblings. The tool groups root children purely by name, so Logs merges
through the same level-1 pass as the date folders.
"""

import copy
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "dedup_drive_folders", ROOT / "tools" / "dedup_drive_folders.py"
)
dedup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dedup)

FOLDER_MIME = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# In-memory fake Drive
# ---------------------------------------------------------------------------


class _Call:
    """Defer mutation to .execute(), like the real API request objects."""

    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result() if callable(self._result) else self._result


class FakeDrive:
    """Minimal in-memory stand-in for the Drive v3 ``files()`` surface.

    Supports exactly the call shapes the dedup tool uses: ``list`` with a
    parent + ``trashed = false`` query (optionally folders-only) ordered by
    ``createdTime``, and ``update`` for moves (addParents/removeParents)
    and trashing (body={"trashed": True}).
    """

    def __init__(self):
        self.store: dict[str, dict] = {}
        self._n = 0

    # -- tree builders ------------------------------------------------------

    def add_folder(self, name: str, parent: str, *, created: str) -> str:
        return self._add(name, parent, FOLDER_MIME, created)

    def add_file(self, name: str, parent: str, *, created: str = "2026-01-01T00:00:00Z") -> str:
        return self._add(name, parent, "text/plain", created)

    def _add(self, name: str, parent: str, mime: str, created: str) -> str:
        self._n += 1
        fid = f"f{self._n:03d}"
        self.store[fid] = {
            "id": fid,
            "name": name,
            "mimeType": mime,
            "createdTime": created,
            "parent": parent,
            "trashed": False,
        }
        return fid

    # -- assertion helpers --------------------------------------------------

    def children(self, parent_id: str) -> list[str]:
        """Names of non-trashed children, sorted."""
        return sorted(
            f["name"] for f in self.store.values() if f["parent"] == parent_id and not f["trashed"]
        )

    def snapshot(self) -> dict:
        return copy.deepcopy(self.store)

    # -- Drive API surface --------------------------------------------------

    def files(self):
        return self

    def list(self, *, q, spaces=None, fields=None, orderBy=None, pageSize=None, pageToken=None):
        parent_id = re.search(r"'([^']+)' in parents", q).group(1)
        folders_only = "mimeType" in q
        rows = [
            f
            for f in self.store.values()
            if f["parent"] == parent_id
            and not f["trashed"]
            and (not folders_only or f["mimeType"] == FOLDER_MIME)
        ]
        assert orderBy == "createdTime", "tool must list oldest-first"
        rows.sort(key=lambda f: f["createdTime"])
        return _Call({"files": [dict(f) for f in rows]})

    def update(self, *, fileId, addParents=None, removeParents=None, body=None, fields=None):
        f = self.store[fileId]

        def _apply():
            if addParents:
                # The tool always moves a child out of the duplicate it lives in.
                assert f["parent"] == removeParents
                f["parent"] = addParents
            if body and body.get("trashed"):
                f["trashed"] = True
            return {"id": fileId}

        return _Call(_apply)


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest.fixture
def drive():
    return FakeDrive()


def run_tool(monkeypatch, drive: FakeDrive, *argv: str) -> None:
    monkeypatch.setattr(dedup, "build_service", lambda *a, **k: drive)
    monkeypatch.setattr(dedup, "GDRIVE_ROOT_FOLDER_ID", "root")
    monkeypatch.setattr(sys, "argv", ["dedup_drive_folders.py", *argv])
    assert dedup.main() == 0


# ---------------------------------------------------------------------------
# Logs folder duplicates (the "Logs", "Logs (1)" … mirror rendering)
# ---------------------------------------------------------------------------


class TestLogsMerge:
    def test_apply_merges_into_oldest_and_trashes_dupes(self, monkeypatch, drive):
        oldest = drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        dupe1 = drive.add_folder("Logs", "root", created="2026-07-01T00:00:00Z")
        dupe2 = drive.add_folder("Logs", "root", created="2026-07-10T00:00:00Z")
        drive.add_file("2026-06-01.log", oldest)
        drive.add_file("2026-06-02.log", oldest)
        drive.add_file("2026-07-05.log", dupe1)
        drive.add_file("2026-07-16.log", dupe2)

        run_tool(monkeypatch, drive, "--apply")

        assert drive.children(oldest) == [
            "2026-06-01.log",
            "2026-06-02.log",
            "2026-07-05.log",
            "2026-07-16.log",
        ]
        assert not drive.store[oldest]["trashed"]
        assert drive.store[dupe1]["trashed"]
        assert drive.store[dupe2]["trashed"]

    def test_same_log_filename_is_a_conflict_left_in_place(self, monkeypatch, drive, capsys):
        # The same day's log can exist in two copies — one truncated, one
        # complete. Which is the good one isn't knowable here, so the tool
        # must report, not guess.
        oldest = drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        dupe = drive.add_folder("Logs", "root", created="2026-07-01T00:00:00Z")
        keeper_log = drive.add_file("2026-07-08.log", oldest)
        conflict_log = drive.add_file("2026-07-08.log", dupe)
        drive.add_file("2026-07-12.log", dupe)

        run_tool(monkeypatch, drive, "--apply")

        # The non-conflicting file moved; the conflicting one stayed put.
        assert drive.children(oldest) == ["2026-07-08.log", "2026-07-12.log"]
        assert drive.store[keeper_log]["parent"] == oldest
        assert drive.store[conflict_log]["parent"] == dupe
        # A duplicate still holding a conflict must NOT be trashed.
        assert not drive.store[dupe]["trashed"]
        out = capsys.readouterr().out
        assert "conflict" in out
        assert "2026-07-08.log" in out

    def test_dry_run_changes_nothing(self, monkeypatch, drive, capsys):
        oldest = drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        drive.add_folder("Logs", "root", created="2026-07-01T00:00:00Z")
        drive.add_file("2026-06-01.log", oldest)
        before = drive.snapshot()

        run_tool(monkeypatch, drive)  # no --apply

        assert drive.store == before
        out = capsys.readouterr().out
        assert "DRY RUN" in out
        assert "'Logs': 2 copies" in out

    def test_single_logs_folder_is_untouched(self, monkeypatch, drive, capsys):
        logs = drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        drive.add_file("2026-06-01.log", logs)
        before = drive.snapshot()

        run_tool(monkeypatch, drive, "--apply")

        assert drive.store == before
        assert "Logs" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Date + company folders (the PR #163 scatter) merged in the same sweep
# ---------------------------------------------------------------------------


class TestFullSweep:
    def test_date_company_and_logs_dupes_merge_in_one_run(self, monkeypatch, drive):
        # Three copies of one date folder under the root.
        d_old = drive.add_folder("2026-07-06", "root", created="2026-07-06T01:00:00Z")
        d_new1 = drive.add_folder("2026-07-06", "root", created="2026-07-06T02:00:00Z")
        d_new2 = drive.add_folder("2026-07-06", "root", created="2026-07-06T03:00:00Z")
        acme1 = drive.add_folder("Acme", d_old, created="2026-07-06T01:10:00Z")
        drive.add_file("CV_EN.pdf", acme1)
        beta = drive.add_folder("Beta", d_new1, created="2026-07-06T02:10:00Z")
        drive.add_file("CV_EN.pdf", beta)
        drive.add_file("stray.txt", d_new2)
        # Two copies of a company folder inside the surviving date folder.
        acme2 = drive.add_folder("Acme", d_old, created="2026-07-06T04:00:00Z")
        drive.add_file("job_posting.txt", acme2)
        # And a Logs duplicate pair alongside.
        logs_old = drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        logs_new = drive.add_folder("Logs", "root", created="2026-07-01T00:00:00Z")
        drive.add_file("2026-07-16.log", logs_new)

        run_tool(monkeypatch, drive, "--apply")

        # Date level: everything converged on the oldest copy, dupes trashed.
        # "Acme" existed twice inside d_old already, so the level-2 pass then
        # merged acme2's unique child into acme1.
        assert drive.children("root") == ["2026-07-06", "Logs"]
        assert not drive.store[d_old]["trashed"]
        assert drive.store[d_new1]["trashed"]
        assert drive.store[d_new2]["trashed"]
        assert drive.children(d_old) == ["Acme", "Beta", "stray.txt"]
        assert drive.children(acme1) == ["CV_EN.pdf", "job_posting.txt"]
        assert drive.store[acme2]["trashed"]
        # Logs level: merged into the oldest copy.
        assert drive.children(logs_old) == ["2026-07-16.log"]
        assert drive.store[logs_new]["trashed"]

    def test_output_is_ascii_only(self, monkeypatch, drive, capsys):
        # Windows cp1252 console — the tool must never print non-ASCII.
        drive.add_folder("Logs", "root", created="2026-06-01T00:00:00Z")
        d = drive.add_folder("Logs", "root", created="2026-07-01T00:00:00Z")
        drive.add_file("2026-07-16.log", d)

        run_tool(monkeypatch, drive, "--apply")

        capsys.readouterr().out.encode("ascii")  # raises if non-ASCII slipped in
