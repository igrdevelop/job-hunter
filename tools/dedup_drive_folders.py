"""
One-time cleanup of duplicate folders in the Google Drive "Job Hunter" tree.

Background: Drive enforces no unique-name constraint, and the bot resolved
folders with an unsynchronized list-then-create. Concurrent writers (the
post-apply delivery hook, the 30-min upload-missing backfill, each detached
dual-apply shadow process) each created their own "2026-07-06", and later
uploads landed in whichever copy the query happened to return first — so a
day's applications ended up split across "2026-07-06", "2026-07-06 (1)", … as
Drive for Desktop mirrors same-named siblings.

Crucially the split is RECURSIVE: each duplicate date folder holds its own copy
of the same company subfolders (`2026-07-06/Santander/`, `2026-07-06 (1)/
Santander/`), and a single company's files can be scattered across those copies.
So the merge has to recurse — collapse same-named folders at every level and
only stop at genuine file-vs-file name collisions.

hunter/gdrive_client.py now prevents *new* duplicates and always converges on
the oldest copy; this tool merges the *historical* ones already on Drive.

What it does, walking the tree from the root:
  - groups same-named sibling folders under each parent,
  - keeps the OLDEST of each group (the same one the bot now picks),
  - merges every other copy INTO the keeper, recursively: a child folder that
    exists on both sides is merged into the keeper's copy (down to the files);
    a child that exists only in the duplicate is moved over wholesale,
  - a FILE whose name already exists in the keeper is left in place and
    reported as a conflict — never silently overwritten (which copy is the good
    one isn't knowable here),
  - trashes a duplicate folder once it's been fully emptied; a duplicate that
    still holds an unresolved file conflict is kept for manual review.

Trash only — recoverable from Drive's trash for 30 days, never a hard delete.

Run inside the container (it reuses the bot's gsheets_token.json):

    # dry run — prints the merge plan, changes nothing (default):
    docker compose exec job-hunter python tools/dedup_drive_folders.py

    # actually merge:
    docker compose exec job-hunter python tools/dedup_drive_folders.py --apply
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hunter.config import (
    GDRIVE_ROOT_FOLDER_ID,
    GDRIVE_ROOT_FOLDER_NAME,
    GSHEETS_CREDENTIALS_FILE,
    GSHEETS_TOKEN_FILE,
)
from hunter.gdrive_client import _FOLDER_MIME, build_service, get_or_create_folder


def _list_children(svc, parent_id: str) -> list[dict]:
    """Return every non-trashed child of parent_id, oldest first."""
    query = f"'{parent_id}' in parents and trashed = false"

    out: list[dict] = []
    page_token = None
    while True:
        result = (
            svc.files()
            .list(
                q=query,
                spaces="drive",
                fields="nextPageToken, files(id, name, mimeType, createdTime)",
                orderBy="createdTime",
                pageSize=1000,
                pageToken=page_token,
            )
            .execute()
        )
        out.extend(result.get("files", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            return out


def _is_folder(f: dict) -> bool:
    return f.get("mimeType") == _FOLDER_MIME


class Node:
    """One Drive file/folder, with its (folder) children loaded into memory.

    The whole tree is read ONCE up front and every merge is planned against
    these in-memory nodes — so a dry run walks exactly the same code and state
    as --apply and predicts it precisely (an earlier version re-listed from the
    API mid-merge, which made dry-run diverge because nothing was actually
    moved). Real API calls happen only in --apply mode.
    """

    __slots__ = ("id", "name", "is_folder", "children")

    def __init__(self, raw: dict):
        self.id: str = raw["id"]
        self.name: str = raw["name"]
        self.is_folder: bool = _is_folder(raw)
        self.children: list[Node] = []  # populated for folders by _load_tree


def _load_tree(svc, folder_id: str) -> list[Node]:
    """Recursively read folder_id's subtree into Node objects (oldest-first)."""
    nodes: list[Node] = []
    for raw in _list_children(svc, folder_id):
        node = Node(raw)
        if node.is_folder:
            node.children = _load_tree(svc, node.id)
        nodes.append(node)
    return nodes


def _move(svc, node: Node, from_parent: str, keeper: Node, *, apply: bool) -> None:
    if apply:
        svc.files().update(
            fileId=node.id,
            addParents=keeper.id,
            removeParents=from_parent,
            fields="id",
        ).execute()
    keeper.children.append(node)


def _trash(svc, node: Node, *, apply: bool) -> None:
    if apply:
        svc.files().update(fileId=node.id, body={"trashed": True}).execute()


def _merge_into(svc, keeper: Node, dupe: Node, *, apply: bool, indent: str, stats: dict) -> bool:
    """Merge ``dupe``'s children into ``keeper`` (recursively).

    Returns True if ``dupe`` ends up fully emptied — i.e. safe to trash.
    """
    by_name = {c.name: c for c in keeper.children}
    fully_absorbed = True

    for child in list(dupe.children):
        match = by_name.get(child.name)

        if match is None:
            kind = "folder" if child.is_folder else "file"
            print(f"{indent}[>] move {kind} {child.name!r}")
            _move(svc, child, dupe.id, keeper, apply=apply)
            by_name[child.name] = child
            dupe.children.remove(child)
            stats["moved"] += 1

        elif child.is_folder and match.is_folder:
            print(f"{indent}[+] merge {child.name!r}/")
            if _merge_into(svc, match, child, apply=apply, indent=indent + "    ", stats=stats):
                print(f"{indent}    [x] trash emptied {child.name!r}")
                _trash(svc, child, apply=apply)
                dupe.children.remove(child)
                stats["trashed"] += 1
            else:
                fully_absorbed = False

        else:
            # file-vs-file (or a type mismatch): which copy is right is not
            # knowable here, so leave it and keep the parent alive.
            print(f"{indent}[!] conflict, left in place: {child.name!r}")
            stats["conflicts"] += 1
            fully_absorbed = False

    if fully_absorbed:
        print(f"{indent}[x] trash duplicate id={dupe.id}")
        _trash(svc, dupe, apply=apply)
        stats["trashed"] += 1
    else:
        print(f"{indent}[~] keeping duplicate id={dupe.id} (unresolved conflicts)")

    return fully_absorbed


def _dedup_node(svc, parent: Node, *, apply: bool, indent: str, stats: dict) -> None:
    """Collapse same-named folder siblings under ``parent``, then recurse into keepers."""
    by_name: dict[str, list[Node]] = defaultdict(list)
    for c in parent.children:
        if c.is_folder:
            by_name[c.name].append(c)

    for name, group in sorted(by_name.items()):
        keeper, *dupes = group  # oldest first (tree is loaded createdTime-ordered)
        for dupe in dupes:
            if dupe is group[1]:  # print the header once, before the first dupe
                print(f"{indent}{name!r}: {len(group)} copies - keeping oldest id={keeper.id}")
            if _merge_into(svc, keeper, dupe, apply=apply, indent=indent + "  ", stats=stats):
                parent.children.remove(dupe)
        # Recurse into the keeper only — the duplicates are merged away. Catches
        # same-named dups nested deeper (e.g. two 'Nexters' inside one date).
        _dedup_node(svc, keeper, apply=apply, indent=indent + "  ", stats=stats)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    parser.add_argument(
        "--apply",
        action="store_true",
        help="actually merge (default: dry run, print the plan only)",
    )
    args = parser.parse_args()

    svc = build_service(GSHEETS_CREDENTIALS_FILE, GSHEETS_TOKEN_FILE)
    root_id = GDRIVE_ROOT_FOLDER_ID or get_or_create_folder(svc, GDRIVE_ROOT_FOLDER_NAME, None)

    mode = "APPLY" if args.apply else "DRY RUN (use --apply to merge)"
    print(f"=== Drive duplicate folder cleanup - {mode} ===")
    print(f"root: {root_id}\n")

    root = Node({"id": root_id, "name": "<root>", "mimeType": _FOLDER_MIME})
    root.children = _load_tree(svc, root_id)

    stats = {"moved": 0, "trashed": 0, "conflicts": 0}
    _dedup_node(svc, root, apply=args.apply, indent="", stats=stats)

    print("\n=== Summary ===")
    print(f"items moved              : {stats['moved']}")
    print(f"duplicate folders trashed: {stats['trashed']}")
    print(f"file conflicts left      : {stats['conflicts']}")
    if not args.apply and (stats["moved"] or stats["trashed"]):
        print("\nDry run - nothing was changed. Re-run with --apply to merge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
