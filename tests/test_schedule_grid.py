"""Night-weighted hunt grid + quiet hours (hunter/schedules/grid.py).

Owner ask (2026-08-16): more hunts at night, nothing at all between 18:00 and
00:00. Dropping the 19:00 base time cannot express that on its own — 25 sources
at 40 min span 16h40m, so the 08:00 and 13:00 cycles wrap into the evening by
themselves. These tests pin the blackout walk and, crucially, that the walk is
a no-op when no blackout is configured.
"""

from __future__ import annotations

from hunter.schedules.grid import (
    MINUTES_PER_DAY,
    blackout_covers_day,
    fire_minute,
    parse_blackout,
    parse_hhmm,
)

EVENING = "18:00-00:00"
SOURCES = 25
OFFSET = 40
BASES = ["02:00", "05:00", "08:00", "13:00"]


def _minutes(spec: str) -> int:
    hh, mm = spec.split(":")
    return int(hh) * 60 + int(mm)


# ── parse_hhmm ────────────────────────────────────────────────────────────────


def test_parse_hhmm_valid():
    assert parse_hhmm("18:00") == 1080
    assert parse_hhmm("00:00") == 0
    assert parse_hhmm(" 07:45 ") == 465


def test_parse_hhmm_24_00_wraps_to_zero():
    assert parse_hhmm("24:00") == 0


def test_parse_hhmm_rejects_garbage():
    for bad in ("", "nope", "25:00", "12:99", "12", None):
        assert parse_hhmm(bad) is None


# ── parse_blackout ────────────────────────────────────────────────────────────


def test_parse_blackout_evening():
    # "00:00" as an END means end-of-day, not a zero-width window — writing
    # "the whole evening" as 18:00-00:00 is the natural spelling.
    assert parse_blackout(EVENING) == [(1080, MINUTES_PER_DAY)]


def test_parse_blackout_accepts_24_00_end():
    assert parse_blackout("18:00-24:00") == [(1080, MINUTES_PER_DAY)]


def test_parse_blackout_wrapping_window_splits_at_midnight():
    assert parse_blackout("22:00-06:00") == [(1320, MINUTES_PER_DAY), (0, 360)]


def test_parse_blackout_empty_is_no_blackout():
    assert parse_blackout("") == []
    assert parse_blackout("   ") == []


def test_parse_blackout_zero_width_is_no_blackout():
    # "06:00-06:00" must not be read as a whole-day ban.
    assert parse_blackout("06:00-06:00") == []


def test_parse_blackout_garbage_degrades_to_none():
    # A typo in .env must not unschedule the bot — it falls back to the plain
    # modulo grid and warns.
    for bad in ("18:00", "18:00-", "-18:00", "banana", "18:00-99:99"):
        assert parse_blackout(bad) == []


def test_blackout_covers_day():
    assert blackout_covers_day(parse_blackout("00:00-24:00")) is True
    assert blackout_covers_day(parse_blackout(EVENING)) is False
    assert blackout_covers_day([]) is False


# ── fire_minute: no blackout == the pre-2026-08-16 formula ────────────────────


def test_no_blackout_reproduces_plain_modulo_grid():
    for base in ("08:00", "13:00", "19:00"):
        for idx in range(SOURCES):
            expected = (_minutes(base) + idx * OFFSET) % MINUTES_PER_DAY
            assert fire_minute(_minutes(base), idx, OFFSET, []) == expected


def test_whole_day_blackout_falls_back_to_modulo_instead_of_hanging():
    segments = parse_blackout("00:00-24:00")
    assert fire_minute(_minutes("08:00"), 5, OFFSET, segments) == (480 + 5 * OFFSET) % 1440


# ── fire_minute: with the evening blackout ────────────────────────────────────


def test_no_slot_ever_lands_in_the_blackout():
    segments = parse_blackout(EVENING)
    for base in BASES:
        for idx in range(SOURCES):
            minute = fire_minute(_minutes(base), idx, OFFSET, segments)
            assert not (1080 <= minute < MINUTES_PER_DAY), f"{base}+{idx} -> {minute}"


def test_every_source_still_gets_a_slot_per_base():
    # Skipping blacked-out slots instead of jumping over them would starve
    # whichever sources happen to sit at those indices.
    segments = parse_blackout(EVENING)
    for base in BASES:
        minutes = [fire_minute(_minutes(base), i, OFFSET, segments) for i in range(SOURCES)]
        assert len(minutes) == SOURCES
        assert all(0 <= m < MINUTES_PER_DAY for m in minutes)


def test_slot_at_the_blackout_edge_jumps_to_the_window_end():
    segments = parse_blackout(EVENING)
    # 13:00 + 7*40 = 17:40 (allowed), the next one would be 18:20 -> 00:00.
    assert fire_minute(_minutes("13:00"), 7, OFFSET, segments) == _minutes("17:40")
    assert fire_minute(_minutes("13:00"), 8, OFFSET, segments) == 0
    assert fire_minute(_minutes("13:00"), 9, OFFSET, segments) == _minutes("00:40")


def test_base_time_inside_the_blackout_is_pushed_to_the_window_end():
    segments = parse_blackout(EVENING)
    assert fire_minute(_minutes("19:00"), 0, OFFSET, segments) == 0


def test_wrapping_blackout_is_honored_on_both_sides_of_midnight():
    segments = parse_blackout("22:00-06:00")
    for idx in range(SOURCES):
        minute = fire_minute(_minutes("08:00"), idx, OFFSET, segments)
        assert 360 <= minute < 1320, f"idx {idx} -> {minute}"


def test_night_share_increases_versus_the_old_schedule():
    """The whole point of the change, as one assertion.

    OLD = bases 08/13/19, no blackout. NEW = bases 02/05/08/13 + evening
    blackout. Measured on prod (87 applies / 14 days) the old grid put ~21% of
    generations into 02:00-08:00; the model agrees, which is why this ratio is
    worth pinning.
    """

    def night_share(bases: list[str], spec: str) -> float:
        segments = parse_blackout(spec)
        slots = [
            fire_minute(_minutes(b), i, OFFSET, segments) for b in bases for i in range(SOURCES)
        ]
        night = [m for m in slots if 120 <= m < 480]
        return len(night) / len(slots)

    old = night_share(["08:00", "13:00", "19:00"], "")
    new = night_share(BASES, EVENING)
    assert old < 0.25
    assert new > 0.30
    assert new > old


# ── register() wiring ─────────────────────────────────────────────────────────


def test_register_schedules_no_hunt_during_quiet_hours(monkeypatch):
    """End-to-end over the real ALL_SOURCES list, not just the arithmetic."""
    import pytz
    from unittest.mock import MagicMock

    from hunter import schedules

    monkeypatch.setattr(schedules, "SCHEDULE_TIMES", BASES)
    monkeypatch.setattr(schedules, "SCHEDULE_BLACKOUT", EVENING)
    monkeypatch.setattr(schedules, "SCHEDULE_SOURCE_OFFSET_MIN", OFFSET)

    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))

    hunt_times = [
        call.kwargs["time"]
        for call in app.job_queue.run_daily.call_args_list
        if (call.kwargs.get("name") or "").startswith("hunt_")
    ]
    assert hunt_times, "no hunt slots registered at all"
    assert not [t for t in hunt_times if t.hour >= 18]

    from hunter.sources import ALL_SOURCES

    assert len(hunt_times) == len(ALL_SOURCES) * len(BASES)


def test_register_ignores_a_whole_day_blackout(monkeypatch):
    """A bot that never hunts is never the intent — fall back, don't go silent."""
    import pytz
    from unittest.mock import MagicMock

    from hunter import schedules

    monkeypatch.setattr(schedules, "SCHEDULE_TIMES", ["19:00"])
    monkeypatch.setattr(schedules, "SCHEDULE_BLACKOUT", "00:00-24:00")
    monkeypatch.setattr(schedules, "SCHEDULE_SOURCE_OFFSET_MIN", OFFSET)

    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))

    hunt_times = [
        call.kwargs["time"]
        for call in app.job_queue.run_daily.call_args_list
        if (call.kwargs.get("name") or "").startswith("hunt_")
    ]
    from hunter.sources import ALL_SOURCES

    assert len(hunt_times) == len(ALL_SOURCES)
    # Unchanged modulo grid: source 0 of the 19:00 base still fires at 19:00.
    assert (hunt_times[0].hour, hunt_times[0].minute) == (19, 0)


def test_register_skips_a_malformed_base_time(monkeypatch):
    import pytz
    from unittest.mock import MagicMock

    from hunter import schedules

    monkeypatch.setattr(schedules, "SCHEDULE_TIMES", ["02:00", "banana"])
    monkeypatch.setattr(schedules, "SCHEDULE_BLACKOUT", EVENING)
    monkeypatch.setattr(schedules, "SCHEDULE_SOURCE_OFFSET_MIN", OFFSET)

    app = MagicMock()
    schedules.register(app, pytz.timezone("Europe/Warsaw"))

    hunt_names = [
        call.kwargs.get("name")
        for call in app.job_queue.run_daily.call_args_list
        if (call.kwargs.get("name") or "").startswith("hunt_")
    ]
    from hunter.sources import ALL_SOURCES

    assert len(hunt_names) == len(ALL_SOURCES)
