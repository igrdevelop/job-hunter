"""Entry point + scheduling glue for the LinkedIn posts scout (M4).

Two independent tracks (owner decision 2026-07-07):

  python linkedin_scout/run.py --track search   # content-search by keyword, rotates
  python linkedin_scout/run.py --track feed     # plain home-feed scroll, no keyword

Each track owns its own persistent Chrome profile + circuit-breaker state file,
so a trip on one never silences the other and the two never fight over the
same profile lock if a Task Scheduler entry runs them close together.

  python linkedin_scout/run.py --track search --reset   # clear that track's trip
  python linkedin_scout/run.py --reset                  # clear BOTH tracks' trips
  python linkedin_scout/run.py --dry-run                # M1 logic against a fixture,
                                                          # no browser, no send

See linkedin_scout/README.md (M5) for the exact Windows Task Scheduler
registration commands and a plain-English safety-rail summary.
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_BASE_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _BASE_DIR.parent
# `python linkedin_scout/run.py` puts THIS file's directory on sys.path[0], not
# the repo root — without this, neither `import hunter` nor
# `import linkedin_scout` (as a top-level package) would resolve.
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Windows' default console codepage (cp1252) can't encode the emoji used in
# notify.format_message() — reconfigure stdout/stderr to UTF-8 so --dry-run
# (and any console logging of a formatted message) doesn't crash. No-op on
# platforms where this isn't available/needed.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

from linkedin_scout import browser, notify, telegram_relay  # noqa: E402
from linkedin_scout.browser import ScoutCandidate  # noqa: E402
from linkedin_scout.heuristics import LocationVerdict, check_location, is_hiring_post  # noqa: E402
from linkedin_scout.parser import parse_posts  # noqa: E402
from linkedin_scout.seen_store import SeenStore  # noqa: E402
from linkedin_scout.state import ScoutState  # noqa: E402

logger = logging.getLogger("linkedin_scout.run")

DEFAULT_KEYWORDS: tuple[str, ...] = (
    "angular hiring",
    "angular developer",
    "angular praca zdalna",
    "angular Wrocław",
)

_STORAGE_STATE_ENV = "LINKEDIN_STORAGE_STATE"
_KEYWORDS_ENV = "LINKEDIN_SCOUT_KEYWORDS"
_SKIP_CHANCE_ENV = "LINKEDIN_SCOUT_SKIP_CHANCE"
_JITTER_MAX_MIN_ENV = "LINKEDIN_SCOUT_JITTER_MAX_MIN"

_DEFAULT_SKIP_CHANCE = 0.30
_DEFAULT_JITTER_MAX_MIN = 45.0

SEARCH_PROFILE_DIR = _BASE_DIR / ".profile_search"
FEED_PROFILE_DIR = _BASE_DIR / ".profile_feed"
SEARCH_STATE_PATH = _BASE_DIR / "search_state.json"
FEED_STATE_PATH = _BASE_DIR / "feed_state.json"
SEEN_STORE_PATH = _BASE_DIR / "seen_posts.json"
DEFAULT_DRY_RUN_FIXTURE = (
    _REPO_ROOT / "tests" / "fixtures" / "linkedin_scout" / "feed_sample.txt"
)

_TRACKS = ("search", "feed")


def _keywords_from_env() -> list[str]:
    raw = os.environ.get(_KEYWORDS_ENV, "").strip()
    if not raw:
        return list(DEFAULT_KEYWORDS)
    return [kw.strip() for kw in raw.split(",") if kw.strip()]


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("[linkedin_scout] invalid %s=%r — using default %s", name, raw, default)
        return default


def _storage_state_path() -> Path | None:
    raw = os.environ.get(_STORAGE_STATE_ENV, "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def _track_paths(track: str) -> tuple[Path, Path]:
    """(profile_dir, state_path) for a track name."""
    if track == "search":
        return SEARCH_PROFILE_DIR, SEARCH_STATE_PATH
    if track == "feed":
        return FEED_PROFILE_DIR, FEED_STATE_PATH
    raise ValueError(f"unknown track: {track}")


def _maybe_skip_and_jitter(*, skip_chance: float, jitter_max_min: float) -> bool:
    """Task spec §3.5: ~30% chance to no-op, else a random 0-45min sleep
    before opening the browser. Returns True if the run should proceed."""
    if random.random() < skip_chance:
        logger.info(
            "[linkedin_scout] skipped this run (rolled inside the %.0f%% skip chance)",
            skip_chance * 100,
        )
        return False
    jitter_sec = random.uniform(0, jitter_max_min * 60)
    logger.info(
        "[linkedin_scout] jitter sleep: %.1f min before opening the browser",
        jitter_sec / 60,
    )
    time.sleep(jitter_sec)
    return True


def _run_dry_run(fixture_path: Path) -> None:
    """M1 logic against a fixture, no browser, no Telegram send — just print."""
    text = fixture_path.read_text(encoding="utf-8")
    posts = parse_posts(text)
    scouted_at = datetime.now(timezone.utc).isoformat()
    candidates: list[ScoutCandidate] = []
    for post in posts:
        if not is_hiring_post(post.body):
            continue
        if check_location(post.body) is LocationVerdict.REJECT:
            continue
        candidates.append(
            ScoutCandidate(keyword="dry-run", author=post.author, body=post.body, scouted_at=scouted_at)
        )

    print(f"[dry-run] {len(posts)} posts parsed from {fixture_path}, {len(candidates)} would be sent:\n")
    if not candidates:
        print("(no matches)")
    for candidate in candidates:
        print(notify.format_message(candidate))
        print("-" * 40)


def _run_track(track: str, *, headless: bool) -> None:
    profile_dir, state_path = _track_paths(track)
    state = ScoutState(state_path)
    seen_store = SeenStore(SEEN_STORE_PATH)
    storage_state_path = _storage_state_path()

    if track == "search":
        candidates = browser.run_once(
            _keywords_from_env(),
            profile_dir=profile_dir,
            storage_state_path=storage_state_path,
            state=state,
            headless=headless,
        )
    else:
        candidates = browser.run_feed_once(
            profile_dir=profile_dir,
            storage_state_path=storage_state_path,
            state=state,
            headless=headless,
        )

    if not candidates:
        logger.info("[linkedin_scout] %s: 0 candidates this run", track)
        return

    # Owner decision (2026-07-08): "this is just another job source" — relay
    # to the bot over Telegram (the owner's own user session, not the bot's
    # own token — see telegram_relay.py) so the bot's OWN hunt cycle picks it
    # up (hunter/sources/linkedin_scout_relay.py) and runs it through the
    # normal filters/dedup/AUTO_APPLY pipeline, exactly like any other source.
    # A local queue file was tried first and abandoned once it became clear
    # the bot auto-deploys to its own server and doesn't share a filesystem
    # with this script's Windows desktop.
    sent = telegram_relay.send_candidates(candidates, seen_store)
    logger.info(
        "[linkedin_scout] %s: %d candidates, %d relayed to the bot (rest already seen)",
        track, len(candidates), sent,
    )


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="LinkedIn posts scout")
    parser.add_argument(
        "--track", choices=list(_TRACKS), help="which scout track to run"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="clear the circuit-breaker trip for --track (or BOTH tracks if --track omitted)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="run M1 logic against a fixture, no browser, no send",
    )
    parser.add_argument(
        "--fixture",
        type=Path,
        default=DEFAULT_DRY_RUN_FIXTURE,
        help="fixture file for --dry-run",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="debug only — the task spec requires headed Chrome for real runs",
    )
    parser.add_argument(
        "--no-jitter",
        action="store_true",
        help="skip the skip-chance/jitter sleep (manual testing only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    if args.dry_run:
        _run_dry_run(args.fixture)
        return 0

    if args.reset:
        tracks = [args.track] if args.track else list(_TRACKS)
        for track in tracks:
            _, state_path = _track_paths(track)
            ScoutState(state_path).reset()
            logger.info("[linkedin_scout] %s: circuit breaker reset", track)
        return 0

    if not args.track:
        parser.error("--track {search,feed} is required for a real run (or use --dry-run/--reset)")

    if not args.no_jitter:
        skip_chance = _float_env(_SKIP_CHANCE_ENV, _DEFAULT_SKIP_CHANCE)
        jitter_max_min = _float_env(_JITTER_MAX_MIN_ENV, _DEFAULT_JITTER_MAX_MIN)
        if not _maybe_skip_and_jitter(skip_chance=skip_chance, jitter_max_min=jitter_max_min):
            return 0

    _run_track(args.track, headless=args.headless)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
