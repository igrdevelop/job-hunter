"""Root conftest — runs before test collection (before any hunter imports).

Copies .example config files when the personal configs don't exist, so CI
and fresh clones can run the test suite without manual setup.
"""

import shutil
from pathlib import Path

_ROOT = Path(__file__).parent

_EXAMPLE_PAIRS = [
    ("filter_config.example.py", "filter_config.py"),
    ("candidate_config.example.py", "candidate_config.py"),
]

for src_name, dst_name in _EXAMPLE_PAIRS:
    dst = _ROOT / dst_name
    if not dst.exists():
        src = _ROOT / src_name
        if src.exists():
            shutil.copy2(src, dst)
