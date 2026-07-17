"""Tests for tools/dedup_drive_folders.py — historical duplicate-folder cleanup on Drive.

Uses a small in-memory fake Drive (same shape as the googleapiclient service:
``svc.files().list/update(...).execute()``) so the recursive merge is exercised
end to end, and the key invariant — a dry run predicts --apply exactly while
mutating nothing — is checked directly.
"""

import importlib.util
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
_spec = importlib.util.spec_from_file_location(
    "dedup_drive_folders", ROOT / "tools" / "dedup_drive_folders.py"
)
ddf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ddf)

FOLDER = "application/vnd.google-apps.folder"


# ---------------------------------------------------------------------------
# Fake Drive
# ---------------------------------------------------------------------------


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, store):
        self.s = store

    def list(self, q=None, fields=None, orderBy=None, pageSize=None, pageToken=None, spaces=None):
        def run():
            parent = re.search(r"'([^']+)' in parents", q).group(1)
            out = [f for f in self.s.items.values() if parent in f["parents"] and not f["trashed"]]
            out.sort(key=lambda f: f["createdTime"])
            return {"files": out, "nextPageToken": None}

        return _Req(run)

    def update(self, fileId=None, body=None, addParents=None, removeParents=None, fields=None):
        def run():
            f = self.s.items[fileId]
            if body and body.get("trashed"):
                f["trashed"] = True
            if addParents:
                if removeParents in f["parents"]:
                    f["parents"].remove(removeParents)
                f["parents"].append(addParents)
            return {"id": fileId}

        return _Req(run)


class FakeDrive:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self._n = 0
        self._f = _Files(self)

    def files(self):
        return self._f

    def add(self, name, parent, *, folder=True, created=None):
        self._n += 1
        fid = f"id{self._n:03d}"
        self.items[fid] = {
            "id": fid,
            "name": name,
            "parents": [parent] if parent else [],
            "mimeType": FOLDER if folder else "application/octet-stream",
            "createdTime": created or f"2026-07-06T00:00:{self._n:02d}Z",
            "trashed": False,
        }
        return fid

    def tree(self, parent_id) -> dict:
        """Return {name: subtree|None} of non-trashed descendants, for assertions."""
        out: dict = {}
        for f in self.items.values():
            if parent_id in f["parents"] and not f["trashed"]:
                out[f["name"]] = self.tree(f["id"]) if f["mimeType"] == FOLDER else None
        return out


def _run(drive, root_id, *, apply):
    stats = {"moved": 0, "trashed": 0, "conflicts": 0}
    root = ddf.Node({"id": root_id, "name": "<root>", "mimeType": FOLDER})
    root.children = ddf._load_tree(drive, root_id)
    ddf._dedup_node(drive, root, apply=apply, indent="", stats=stats)
    return stats


# ---------------------------------------------------------------------------
# Fixtures: build the real-shape mess once
# ---------------------------------------------------------------------------


def _scattered_drive():
    """3 copies of one date; a company (Santander) whose files are split across
    all three, with a genuine outreach.md file conflict; a company (Schaeffler)
    only in the newest copy; and an empty oldest copy that becomes the keeper.
    """
    d = FakeDrive()
    root = d.add("Job Hunter", None, created="2026-07-01T00:00:00Z")

    keeper = d.add("2026-07-06", root, created="2026-07-06T00:59:00Z")  # oldest, empty
    c1 = d.add("2026-07-06", root, created="2026-07-06T01:00:00Z")
    c2 = d.add("2026-07-06", root, created="2026-07-06T01:00:02Z")
    c3 = d.add("2026-07-06", root, created="2026-07-13T03:45:00Z")

    s1 = d.add("Santander", c1, created="2026-07-06T01:10:00Z")
    d.add("CV_EN_ats88.pdf", s1, folder=False)
    s2 = d.add("Santander", c2, created="2026-07-06T01:10:05Z")
    d.add("Cover_Letter_EN.pdf", s2, folder=False)
    d.add("outreach.md", s2, folder=False)
    s3 = d.add("Santander", c3, created="2026-07-13T03:46:00Z")
    d.add("outreach.md", s3, folder=False)  # collides with s2's outreach.md

    sch = d.add("Schaeffler", c3, created="2026-07-13T03:47:00Z")
    d.add("CV_EN.pdf", sch, folder=False)

    return d, root, keeper


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_consolidates_scattered_company_files():
    d, root, keeper = _scattered_drive()
    stats = _run(d, root, apply=True)

    # The keeper survives; a copy still holding the file conflict is kept too,
    # so two date folders remain (down from four).
    live_dates = [f for f in d.items.values() if f["name"] == "2026-07-06" and not f["trashed"]]
    assert len(live_dates) == 2
    assert keeper in [f["id"] for f in live_dates]
    assert d.items[keeper]["trashed"] is False

    santander = d.tree(keeper)["Santander"]
    # All three scattered files landed in the keeper's single Santander folder.
    assert set(santander) == {"CV_EN_ats88.pdf", "Cover_Letter_EN.pdf", "outreach.md"}
    # Schaeffler moved over wholesale.
    assert "Schaeffler" in d.tree(keeper)

    assert stats["conflicts"] == 1  # the duplicate outreach.md
    assert stats["moved"] >= 3


def test_file_conflict_is_never_overwritten():
    d, root, keeper = _scattered_drive()
    _run(d, root, apply=True)

    # BOTH outreach.md files still exist somewhere non-trashed — the conflicting
    # one was left in place, not silently clobbered.
    live_outreach = [f for f in d.items.values() if f["name"] == "outreach.md" and not f["trashed"]]
    assert len(live_outreach) == 2


def test_dry_run_changes_nothing():
    d, root, _ = _scattered_drive()
    before = {fid: (f["parents"][:], f["trashed"]) for fid, f in d.items.items()}

    _run(d, root, apply=False)

    after = {fid: (f["parents"][:], f["trashed"]) for fid, f in d.items.items()}
    assert before == after


def test_dry_run_stats_match_apply():
    d1, root1, _ = _scattered_drive()
    dry = _run(d1, root1, apply=False)

    d2, root2, _ = _scattered_drive()
    applied = _run(d2, root2, apply=True)

    assert dry == applied


def test_within_folder_duplicates_are_merged():
    # Two 'Nexters' folders inside ONE date folder (no date-level dup) — the
    # recursion into the keeper must still collapse them.
    d = FakeDrive()
    root = d.add("Job Hunter", None, created="2026-07-01T00:00:00Z")
    date = d.add("2026-07-13", root, created="2026-07-13T00:00:00Z")
    n1 = d.add("Nexters", date, created="2026-07-13T01:00:00Z")
    d.add("CV_EN.pdf", n1, folder=False)
    n2 = d.add("Nexters", date, created="2026-07-13T01:00:05Z")
    d.add("outreach.md", n2, folder=False)

    _run(d, root, apply=True)

    nexters = d.tree(date)
    assert list(nexters) == ["Nexters"]
    assert set(nexters["Nexters"]) == {"CV_EN.pdf", "outreach.md"}


def test_no_duplicates_is_a_noop():
    d = FakeDrive()
    root = d.add("Job Hunter", None, created="2026-07-01T00:00:00Z")
    date = d.add("2026-07-14", root, created="2026-07-14T00:00:00Z")
    comp = d.add("Acme", date, created="2026-07-14T01:00:00Z")
    d.add("CV_EN.pdf", comp, folder=False)

    stats = _run(d, root, apply=True)

    assert stats == {"moved": 0, "trashed": 0, "conflicts": 0}
    assert d.tree(root) == {"2026-07-14": {"Acme": {"CV_EN.pdf": None}}}


@pytest.mark.parametrize("apply", [False, True])
def test_empty_duplicate_is_trashed(apply):
    d = FakeDrive()
    root = d.add("Job Hunter", None, created="2026-07-01T00:00:00Z")
    keeper = d.add("2026-07-06", root, created="2026-07-06T01:00:00Z")
    d.add("Acme", keeper, created="2026-07-06T01:10:00Z")
    empty_dupe = d.add("2026-07-06", root, created="2026-07-06T02:00:00Z")

    stats = _run(d, root, apply=apply)

    assert stats["trashed"] == 1
    if apply:
        assert d.items[empty_dupe]["trashed"] is True
        assert d.items[keeper]["trashed"] is False
