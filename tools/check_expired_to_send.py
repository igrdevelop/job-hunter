"""
tools/check_expired_to_send.py — Check all URLs in to_send.xlsx for expiry.

Runs the same parallel logic as the Telegram /check_expired command.
Checks URLs, then immediately tries to apply results to to_send.xlsx.
If the file is open in Excel or LibreOffice Calc, prints a message and exits.

Usage:
    python tools/check_expired_to_send.py
"""

import asyncio
import sys
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


async def main() -> None:
    from hunter.expired_to_send_check import run_check, apply_check, CHECKING_PATH

    print(f"\n[check] === Expired check for to_send.xlsx ===\n")

    async def progress_cb(text: str) -> None:
        print(f"  {text}")

    result = await run_check(progress_cb=progress_cb)

    total   = result["total"]
    alive   = result["alive"]
    expired = result["expired"]
    errors  = result["errors"]

    skipped = result.get("skipped", [])

    print(f"\n{'='*60}")
    print(f"Checked: {total}  |  Alive: {alive}  |  Expired: {len(expired)}  |  Skipped (jobleads): {len(skipped)}  |  Errors: {len(errors)}")

    if expired:
        print(f"\n⏭  EXPIRED ({len(expired)}):")
        for item in expired:
            reason = f" [{item.get('reason', '')}]" if item.get("reason") else ""
            print(f"  • {item['company']} — {item['title']}{reason}")
            print(f"    {item['url']}")

    if errors:
        print(f"\n⚠️  FETCH ERRORS ({len(errors)}):")
        for item in errors:
            print(f"  • {item['company']} — {item['title']}: {item.get('error','')[:100]}")

    if not expired:
        print("\n[check] Nothing expired — to_send.xlsx unchanged.")
        CHECKING_PATH.unlink(missing_ok=True)
        return

    res = apply_check()
    if res["ok"]:
        print("\n[check] ✅ to_send.xlsx updated.")
        synced = res.get("synced", 0)
        if synced:
            print(f"[check] 📊 tracker.xlsx updated — {synced} row(s) marked EXPIRED.")
        if res.get("sync_error"):
            print(f"[check] ⚠️  sync_sent failed: {res['sync_error']}")
    elif res["error"] == "PermissionError":
        print(f"\n[check] ⚠️  to_send.xlsx is open — close Excel / LibreOffice Calc and run again.")
        print(f"[check] Results are saved in: {CHECKING_PATH}")
    else:
        print(f"\n[check] ❌ Failed to apply: {res['error']}")


if __name__ == "__main__":
    asyncio.run(main())
