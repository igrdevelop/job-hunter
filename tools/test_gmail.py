"""
Quick test for Gmail source — runs without starting the full bot.

Usage:
    python tools/test_gmail.py
    python tools/test_gmail.py --hours 72   # look back 72 hours instead of 25
    python tools/test_gmail.py --debug      # also print raw From: and Subject: of every email
"""

import argparse
import sys
from pathlib import Path

# Make sure project root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from hunter.gmail_client import get_gmail_service
from hunter.gmail_parsers import PARSERS
from hunter.sources.gmail import GmailSource, LOOKBACK_HOURS


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--hours", type=int, default=LOOKBACK_HOURS,
                        help=f"How many hours back to look (default: {LOOKBACK_HOURS})")
    parser.add_argument("--debug", action="store_true",
                        help="Print From/Subject of every matched email")
    args = parser.parse_args()

    print(f"Connecting to Gmail...")
    service = get_gmail_service()

    # Build query
    from datetime import datetime, timedelta, timezone
    import base64

    after_ts = int((datetime.now(timezone.utc) - timedelta(hours=args.hours)).timestamp())
    sender_filter = " OR ".join(f"from:{d}" for d in PARSERS)
    query = f"({sender_filter}) after:{after_ts}"

    print(f"Looking back {args.hours} hours")
    print(f"Watching domains: {', '.join(PARSERS.keys())}")
    print(f"Query: {query}\n")

    results = service.users().messages().list(userId="me", q=query, maxResults=100).execute()
    messages = results.get("messages", [])
    print(f"Found {len(messages)} matching emails\n")

    source = GmailSource()
    # Temporarily override lookback
    source_jobs = []
    all_senders = []

    for stub in messages:
        msg = service.users().messages().get(userId="me", id=stub["id"], format="full").execute()
        headers = {h["name"]: h["value"] for h in msg["payload"].get("headers", [])}
        subject = headers.get("Subject", "")
        sender = headers.get("From", "")
        all_senders.append((sender, subject))

        if args.debug:
            print(f"  From: {sender}")
            print(f"  Subject: {subject}")

        jobs = source._parse_message(msg)
        source_jobs.extend(jobs)

        if jobs:
            print(f"✅ {len(jobs)} jobs from: {sender[:60]}")
            for j in jobs:
                print(f"   → {j.url}")
        elif args.debug:
            print(f"⚠️  No jobs parsed from this email")
        if args.debug:
            print()

    print(f"\n{'='*60}")
    print(f"TOTAL: {len(source_jobs)} job URLs found in {len(messages)} emails")

    if not source_jobs:
        print("\nNo jobs found. Possible reasons:")
        print("  1. No emails from these domains in the last", args.hours, "hours")
        print("  2. Email domains don't match — try --debug to see actual From: addresses")
        print("  3. URL pattern in parser doesn't match — share an email example")
        print("\nAll senders found:")
        for sender, subject in all_senders:
            print(f"  {sender}")


if __name__ == "__main__":
    main()
