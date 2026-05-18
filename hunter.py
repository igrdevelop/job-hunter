#!/usr/bin/env python3
"""
hunter.py — Entry point for the Job Hunter Bot.

Usage:
  python hunter.py          # start bot with scheduled hunts
  python hunter.py --now    # start bot AND run one hunt immediately
  python -m hunter          # equivalent alternative
"""
from hunter.__main__ import main

if __name__ == "__main__":
    main()
