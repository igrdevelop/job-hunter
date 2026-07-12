#!/usr/bin/env python3
"""
hunter.py — Legacy entry point shim. Delegates to hunter.__main__.

Kept for backward compatibility with direct `python hunter.py` invocation.
Preferred invocations:
  python -m hunter        (package mode)
  hunter                  (CLI script after pip install)
"""

from hunter.__main__ import main

if __name__ == "__main__":
    main()
