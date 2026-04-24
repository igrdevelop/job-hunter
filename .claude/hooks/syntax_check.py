"""
Hook: PostToolUse — syntax check after Edit/Write on .py files.
Receives tool input as JSON on stdin.
Exits with code 1 if SyntaxError found (Claude sees the error).
"""
import sys
import json
import py_compile
import os


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return  # Can't parse input — don't block

    file_path = data.get("tool_input", {}).get("file_path", "") or data.get("file_path", "")
    if not file_path.endswith(".py"):
        return  # Not a Python file — skip

    if not os.path.exists(file_path):
        return  # File doesn't exist yet — skip

    try:
        py_compile.compile(file_path, doraise=True)
        print(f"[OK] Syntax OK: {os.path.basename(file_path)}")
    except py_compile.PyCompileError as e:
        print(f"[ERROR] SYNTAX ERROR in {os.path.basename(file_path)}:\n{e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
