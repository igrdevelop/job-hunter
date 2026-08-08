"""Generate candidate/filters.example.yaml from builtin_defaults()."""

from __future__ import annotations

from pathlib import Path

import yaml

from hunter.filter_profile import builtin_defaults, clear_profile_cache

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "candidate" / "filters.example.yaml"

HEADER = """\
# filters.example.yaml — copy to filters.yaml next to candidate.yaml
# (or users/{uid}/candidate/filters.yaml once multi-user B3.5 is live).
#
# Docs: docs/FILTERS_YAML_PLAN.md
# Every key below is optional — omit a key to keep the shared Layer-1 default.
# Missing file entirely = today's owner behavior byte-for-byte.
#
# Merge strategies:
#   replace  — your value fully replaces the default (most keys)
#   extend   — you can only ADD entries (exclude_companies, extra_anti_hybrid_cities)
#
# Derived (NOT set here — come from candidate.yaml):
#   locations = remote/zdalnie + location.home_city_aliases
#
# Legacy aliases still accepted if you prefer them:
#   require_angular: true  -> require_title_terms: [angular]
#   exclude_react_without_angular: false -> exclude_stacks_without: null
#
# After editing: the next /hunt picks the change up (mtime cache). No deploy.
#
# --- defaults below match hunter.filter_profile.builtin_defaults() ---

"""


def main() -> None:
    clear_profile_cache()
    data = builtin_defaults()
    # Canonical knobs only — legacy bools are synced by the loader.
    skip = {"locations", "require_angular", "exclude_react_without_angular"}
    out = {k: v for k, v in data.items() if k not in skip}
    body = yaml.safe_dump(out, allow_unicode=True, default_flow_style=False, sort_keys=False)
    OUT.write_text(HEADER + body, encoding="utf-8")
    print(f"wrote {len(out)} keys -> {OUT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
