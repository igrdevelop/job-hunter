"""
Hook: PreToolUse — block edits to protected files (.env, tracker.xlsx).
Receives tool input as JSON on stdin.
Exits with code 1 to block the tool call.
"""
import sys
import json
import os


PROTECTED = [".env", "tracker.xlsx"]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # Can't parse input — don't block

    file_path = (data.get("tool_input", {}).get("file_path", "") or data.get("file_path", "")).replace("\\", "/")
    basename = os.path.basename(file_path)

    for name in PROTECTED:
        if basename == name or file_path.endswith("/" + name):
            print(
                f"\nBLOCKED: Editing '{name}' is not allowed.\n"
                f"   Reason: see CLAUDE.md -- this file contains sensitive data.\n"
                f"   If you really need to edit it, do it manually.",
                file=sys.stderr,
            )
            sys.exit(1)


if __name__ == "__main__":
    main()
