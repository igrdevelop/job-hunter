"""
Unit tests for hunter/sent_parse.py — parsing the messy Sent column into a date.

Cases mirror the real values seen in the production sheet.
"""

from datetime import date

import pytest

from hunter.sent_parse import classify, parse_sent_date

_Y = date.today().year  # default year used when Sent has no year ("15 05", "1305")


class TestParseSentDate:
    @pytest.mark.parametrize("value, expected", [
        ("08 04 26", date(2026, 4, 8)),          # DD MM YY
        ("09 04 26", date(2026, 4, 9)),
        ("10 04 26", date(2026, 4, 10)),
        ("2026-07-04 00:00:00", date(2026, 7, 4)),  # ISO timestamp
        ("2026-07-05", date(2026, 7, 5)),
        ("15 05", date(_Y, 5, 15)),              # DD MM (current year)
        ("13 05", date(_Y, 5, 13)),
        ("1305", date(_Y, 5, 13)),               # compact DDMM
        ("22 05 (21 05)", date(_Y, 5, 22)),      # first pair wins
        ("24.04.2026", date(2026, 4, 24)),       # DD.MM.YYYY
        ("Zaaplikowano na tę ofertę 24.04.2026", date(2026, 4, 24)),
        ("Applied on May 16, 2026", date(2026, 5, 16)),
    ])
    def test_dates_parse(self, value, expected):
        assert parse_sent_date(value) == expected

    @pytest.mark.parametrize("value", [
        "", "   ", "—", "-", " - ",
        "выгасла", "wygasła", "Oferta wygasła", "EXPIRED", "Offer expired",
        "inactive", "No longer accepting applications",
        "Pracodawca zakończył zbieranie zgłoszeń na tę ofertę",
        "Ta oferta nie jest już dostępna",
        "Реакт", "повторка", "не тот стек", "немецкий", "тестер",
        "гибрид Краков", "бекенд",
        "x/3/2026",        # leading letter → not a date
        "10 months ago",   # no real day/month pair
        "1 месяц проект",
    ])
    def test_non_dates_return_none(self, value):
        assert parse_sent_date(value) is None

    def test_invalid_calendar_date_rejected(self):
        # 99 is not a valid day → None, not a crash.
        assert parse_sent_date("99 99 26") is None


class TestClassify:
    @pytest.mark.parametrize("value, bucket", [
        ("08 04 26", "applied"),
        ("2026-07-04 00:00:00", "applied"),
        ("EXPIRED", "expired"),
        ("выгасла", "expired"),
        ("No longer accepting applications", "expired"),
        ("Реакт", "other"),
        ("не тот стек", "other"),
        ("", "blank"),
        ("—", "blank"),
        ("- ", "blank"),
    ])
    def test_buckets(self, value, bucket):
        assert classify(value) == bucket
