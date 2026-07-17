"""
One-time cleanup of duplicate folders in the Google Drive "Job Hunter" tree.

Background: Drive enforces no unique-name constraint, and the bot resolved
folders with an unsynchronized list-then-create. Concurrent writers (the
post-apply delivery hook, the 30-min upload-missing backfill, each detached
dual-apply shadow process) each created their own "2026-07-06", and later
uploads landed in whichever copy the query happened to return first — so the
day's files ended up scattered across "2026-07-06", "2026-07-06 (1)", … as
Drive for Desktop mirrors them.

hunter/gdrive_client.py now prevents *new* duplicates and always converges on
the oldest copy; this tool merges the *historical* ones already on Drive.

What it does, per parent (the root, then each date folder):
  - groups non-trashed child folders by name,
  - keeps the OLDEST of each group (the same one the bot now picks),
  - moves the other copies' children into the keeper — a child whose name
    already exists in the keeper is left in place and reported as a conflict
    rather than merged, so nothing is silently overwritten,
  - trashes the emptied duplicate (recoverable from Drive's trash for 30 days;
    never a permanent delete),
  - recurses one level so duplicate company folders inside a date are merged too.

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


def _list_children(svc, parent_id: str, *, folders_only: bool = False) -> list[dict]:
    """Return every non-trashed child of parent_id, oldest first."""
    query = f"'{parent_id}' in parents and trashed = false"
    if folders_only:
        query += f" and mimeType = '{_FOLDER_MIME}'"

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


def _duplicate_groups(svc, parent_id: str) -> list[tuple[str, list[dict]]]:
    """Return [(name, [folders oldest-first])] for names appearing more than once."""
    by_name: dict[str, list[dict]] = defaultdict(list)
    for f in _list_children(svc, parent_id, folders_only=True):
        by_name[f["name"]].append(f)
    return [(name, group) for name, group in sorted(by_name.items()) if len(group) > 1]


def _merge_group(svc, name: str, group: list[dict], *, apply: bool, indent: str) -> dict:
    """Move every duplicate's children into the oldest copy, then trash it."""
    keeper, *dupes = group
    stats = {"merged_folders": 0, "moved_children": 0, "conflicts": 0}

    keeper_children = {c["name"] for c in _list_children(svc, keeper["id"])}
    print(f"{indent}{name!r}: {len(group)} copies - keeping oldest id={keeper['id']}")

    for dupe in dupes:
        left_behind = 0
        for child in _list_children(svc, dupe["id"]):
            if child["name"] in keeper_children:
                # Same name on both sides. Which copy is the good one isn't
                # knowable here, so leave it for the owner to look at.
                print(f"{indent}  [!] conflict, left in place: {child['name']!r}")
                stats["conflicts"] += 1
                left_behind += 1
                continue
            print(f"{indent}  [>] move {child['name']!r}")
            if apply:
                svc.files().update(
                    fileId=child["id"],
                    addParents=keeper["id"],
                    removeParents=dupe["id"],
                    fields="id",
                ).execute()
            keeper_children.add(child["name"])
            stats["moved_children"] += 1

        if left_behind:
            # Trashing would take the unmerged children along with it.
            print(f"{indent}  [~] keeping duplicate id={dupe['id']} ({left_behind} conflict(s))")
            continue

        print(f"{indent}  [x] trash duplicate id={dupe['id']}")
        if apply:
            svc.files().update(fileId=dupe["id"], body={"trashed": True}).execute()
        stats["merged_folders"] += 1

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
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

    totals = {"merged_folders": 0, "moved_children": 0, "conflicts": 0}

    def _accumulate(stats: dict) -> None:
        for k, v in stats.items():
            totals[k] += v

    # Level 1: duplicate date folders under the root.
    date_dupes = _duplicate_groups(svc, root_id)
    for name, group in date_dupes:
        _accumulate(_merge_group(svc, name, group, apply=args.apply, indent=""))

    # Level 2: duplicate company folders inside each (surviving) date folder.
    # Re-listed after the merge above so the keepers hold the full child set.
    for date_folder in _list_children(svc, root_id, folders_only=True):
        company_dupes = _duplicate_groups(svc, date_folder["id"])
        if not company_dupes:
            continue
        print(f"\n{date_folder['name']}/")
        for name, group in company_dupes:
            _accumulate(_merge_group(svc, name, group, apply=args.apply, indent="  "))

    print("\n=== Summary ===")
    print(f"duplicate folders merged : {totals['merged_folders']}")
    print(f"children moved           : {totals['moved_children']}")
    print(f"conflicts left in place  : {totals['conflicts']}")
    if not args.apply and (totals["merged_folders"] or totals["moved_children"]):
        print("\nDry run - nothing was changed. Re-run with --apply to merge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
