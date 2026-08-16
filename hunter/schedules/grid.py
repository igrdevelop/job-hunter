"""
hunter/schedules/grid.py — hunt-slot time arithmetic (pure, no PTB imports).

`register()` lays 25 sources out on a per-source staggered grid: source *i* of
base time *T* fires at `T + i * SCHEDULE_SOURCE_OFFSET_MIN`. One cycle of 25
sources at 40 min spans 16h40m, so the tail of a daytime cycle used to wrap
deep into the evening and past midnight — dropping a base time cannot express
"never run between 18:00 and 00:00" on its own.

This module adds a blackout window the walk JUMPS OVER: offsets accumulate
through ALLOWED minutes only. Every source keeps a slot (skipping the ones
that land in the gap would silently starve whichever sources sit at those
indices) and their relative order is preserved; the only distortion is that
sources on either side of the gap sit closer together in wall-clock time.

Kept free of telegram/pytz imports so the whole grid is unit-testable as
plain integers.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MINUTES_PER_DAY = 24 * 60

# One (start, end) pair per non-wrapping blackout segment, in minutes since
# local midnight; `end` is exclusive.
Segments = list[tuple[int, int]]


def parse_hhmm(value: str) -> int | None:
    """ "18:00" -> 1080. None when the value isn't a valid HH:MM."""
    try:
        hh, mm = (int(part) for part in value.strip().split(":"))
    except (ValueError, AttributeError):
        return None
    if not (0 <= hh <= 24 and 0 <= mm < 60):
        return None
    return (hh * 60 + mm) % MINUTES_PER_DAY


def parse_blackout(spec: str) -> Segments:
    """Parse "HH:MM-HH:MM" into non-wrapping [start, end) minute segments.

    An end of "00:00" means end-of-day, not an empty window ("18:00-00:00" is
    the natural way to write "the whole evening"). A window that wraps midnight
    ("22:00-06:00") becomes two segments. Anything unparseable yields no
    segments — a malformed value must not silently unschedule the bot, so the
    caller falls back to the plain modulo grid.
    """
    if not spec or not spec.strip():
        return []

    raw_start, sep, raw_end = spec.partition("-")
    if not sep:
        logger.warning("[Schedule] Invalid SCHEDULE_BLACKOUT=%r (expected HH:MM-HH:MM)", spec)
        return []

    start = parse_hhmm(raw_start)
    end = parse_hhmm(raw_end)
    if start is None or end is None:
        logger.warning("[Schedule] Invalid SCHEDULE_BLACKOUT=%r (expected HH:MM-HH:MM)", spec)
        return []

    # "18:00-00:00" / "18:00-24:00" — both mean "until end of day".
    if end == 0:
        end = MINUTES_PER_DAY

    # Compare against the PROMOTED end: "00:00-24:00" is a whole-day ban (left
    # for blackout_covers_day to reject loudly), while "06:00-06:00" is a
    # zero-width no-op. Comparing `end % MINUTES_PER_DAY` would conflate them.
    if start == end:
        return []
    if start < end:
        return [(start, end)]
    # Wraps midnight: "22:00-06:00" -> [22:00, 24:00) + [00:00, 06:00)
    return [(start, MINUTES_PER_DAY), (0, end)]


def blackout_covers_day(segments: Segments) -> bool:
    """True when the segments leave no allowed minute at all.

    A whole-day blackout would make the offset walk loop forever, so callers
    treat it as "no blackout" rather than hanging or scheduling nothing.
    """
    return sum(end - start for start, end in segments) >= MINUTES_PER_DAY


def _skip_blackout(minute: int, segments: Segments) -> int:
    """Advance `minute` to the first allowed minute at or after it."""
    # Two passes are enough for the segments parse_blackout can produce (at
    # most one wrap), but the bound keeps a hand-built segment list from
    # spinning here.
    for _ in range(len(segments) + 2):
        for start, end in segments:
            if start <= minute < end:
                minute = end % MINUTES_PER_DAY
                break
        else:
            return minute
    return minute


def fire_minute(base_minute: int, index: int, offset_min: int, segments: Segments) -> int:
    """Minute-of-day for source `index` of the cycle starting at `base_minute`.

    Without a blackout this is exactly the old `(base + index * offset) % 1440`.
    With one, each step lands on an allowed minute: the offset is added, and if
    the result falls inside the blackout the slot moves to the window's end.
    """
    if not segments or blackout_covers_day(segments):
        return (base_minute + index * offset_min) % MINUTES_PER_DAY

    minute = _skip_blackout(base_minute % MINUTES_PER_DAY, segments)
    for _ in range(max(0, index)):
        minute = _skip_blackout((minute + offset_min) % MINUTES_PER_DAY, segments)
    return minute
