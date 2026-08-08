"""Job-matching filter rules — shim over ``hunter.filter_profile``.

The FILTER dict values + WHY-comments live in
``hunter.filter_profile.builtin_defaults()`` (docs/FILTERS_YAML_PLAN.md M1).
This module keeps the historical import surface:

    from hunter.filter_config import FILTER
    from hunter.config import FILTER  # re-export

so every existing caller stays unchanged. ``FILTER = load_profile()`` with no
user file is byte-for-byte today's builtin defaults (locations still derived
from candidate.yaml).

To tune shared defaults: edit ``builtin_defaults()`` in filter_profile.py.
To tune per-user policy (M3+): write ``filters.yaml`` next to candidate.yaml.
"""

from hunter.filter_profile import load_profile

FILTER = load_profile()
