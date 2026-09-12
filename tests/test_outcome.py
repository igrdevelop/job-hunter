"""Recording what happened to a sent application (outcome_label + /outcome).

docs/improvement-2026-09/08-DATA_EVAL_PLAN.md M1, owner decision 2026-09-12.
A 90-day funnel run on prod found 399 sent applications and ZERO recorded
outcomes: nothing in the codebase ever wrote the free-text `answer` column, so
every metric past "sent" — including "does the ATS verdict predict a reply" —
was structurally unmeasurable. These tests pin the capture side and the one
distinction the whole feature exists for: "no replies" vs "nobody recorded
anything".
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from hunter import funnel
from hunter.db import get_db


@pytest.fixture()
def db(tracker_db, monkeypatch):
    monkeypatch.setattr(funnel, "DB_PATH", tracker_db)
    monkeypatch.setenv("JOB_HUNTER_USER_ID", "u1")
    return tracker_db


def _insert(
    db,
    *,
    user="u1",
    url=None,
    sent="",
    ats="85%",
    label="",
    answer="",
    row_id=None,
    company="Acme",
):
    url = url or f"https://jobs.example.com/{uuid.uuid4().hex[:8]}"
    row_id = row_id or uuid.uuid4().hex[:8]
    with get_db(db) as conn:
        conn.execute(
            "INSERT INTO applications (id, date, company, title, ats_status, url, url_norm, "
            "sent, answer, outcome_label, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                row_id,
                date.today().isoformat(),
                company,
                "Dev",
                ats,
                url,
                url,
                sent,
                answer,
                label,
                user,
            ),
        )
    return row_id, url


def _row(db, row_id):
    with get_db(db) as conn:
        return dict(conn.execute("SELECT * FROM applications WHERE id=?", (row_id,)).fetchone())


# ── labels ────────────────────────────────────────────────────────────────────


def test_labels_are_the_four_agreed_values():
    from hunter.tracker import OUTCOME_LABELS, OUTCOME_REPLY_LABELS

    assert set(OUTCOME_LABELS) == {"interview", "rejected", "offer", "silence"}
    assert "silence" not in OUTCOME_REPLY_LABELS, "silence is observed, but it is not a reply"
    assert OUTCOME_REPLY_LABELS.issubset(OUTCOME_LABELS)


def test_set_outcome_rejects_an_unknown_label(db):
    from hunter.tracker import set_outcome

    row_id, _ = _insert(db, sent="2026-09-01")
    with pytest.raises(ValueError):
        set_outcome(row_id, "ghosted")
    assert _row(db, row_id)["outcome_label"] == ""


# ── set_outcome ───────────────────────────────────────────────────────────────


def test_set_outcome_by_row_id_stamps_time_and_marks_dirty(db):
    from hunter.tracker import set_outcome

    row_id, _ = _insert(db, sent="2026-09-01")
    assert set_outcome(row_id, "interview") is True
    row = _row(db, row_id)
    assert row["outcome_label"] == "interview"
    assert row["outcome_at"], "outcome_at must be stamped"
    assert row["sheets_dirty"] == 1, "the Sheet mirror has to pick the change up"


def test_set_outcome_by_url(db):
    from hunter.tracker import set_outcome

    row_id, url = _insert(db, sent="2026-09-01")
    assert set_outcome(url, "rejected") is True
    assert _row(db, row_id)["outcome_label"] == "rejected"


def test_set_outcome_label_is_case_insensitive(db):
    from hunter.tracker import set_outcome

    row_id, _ = _insert(db, sent="2026-09-01")
    assert set_outcome(row_id, "  Offer ") is True
    assert _row(db, row_id)["outcome_label"] == "offer"


def test_clearing_an_outcome_removes_label_and_timestamp(db):
    from hunter.tracker import set_outcome

    row_id, _ = _insert(db, sent="2026-09-01")
    set_outcome(row_id, "silence")
    assert set_outcome(row_id, "") is True
    row = _row(db, row_id)
    assert row["outcome_label"] == ""
    assert row["outcome_at"] is None


def test_set_outcome_unknown_key_returns_false(db):
    from hunter.tracker import set_outcome

    assert set_outcome("deadbeef", "offer") is False
    assert set_outcome("https://nowhere.example.com/x", "offer") is False
    assert set_outcome("", "offer") is False


def test_set_outcome_never_touches_another_users_row(db, monkeypatch):
    """Two users applied to the same URL. Recording one outcome must update the
    caller's row only — a leak here writes someone else's application history.
    """
    from hunter.tracker import set_outcome

    shared = "https://jobs.example.com/shared-role"
    mine, _ = _insert(db, user="u1", url=shared, sent="2026-09-01")
    theirs, _ = _insert(db, user="u2", url=shared, sent="2026-09-01")

    monkeypatch.setenv("JOB_HUNTER_USER_ID", "u1")
    assert set_outcome(shared, "offer") is True
    assert _row(db, mine)["outcome_label"] == "offer"
    assert _row(db, theirs)["outcome_label"] == "", "another user's row was written"

    # And by row id: u1 cannot label u2's row even knowing its id.
    assert set_outcome(theirs, "rejected") is False
    assert _row(db, theirs)["outcome_label"] == ""


# ── get_rows_awaiting_outcome ────────────────────────────────────────────────


def test_awaiting_lists_only_sent_unlabelled_rows_of_this_user(db):
    from hunter.tracker import get_rows_awaiting_outcome

    waiting, _ = _insert(db, sent="2026-09-01", company="Waiting")
    _insert(db, sent="2026-09-01", label="interview", company="Labelled")
    _insert(db, sent="", company="NotSent")
    _insert(db, sent="—", company="Skipped")
    _insert(db, user="u2", sent="2026-09-01", company="OtherUser")

    rows = get_rows_awaiting_outcome(limit=10)
    assert [r["company"] for r in rows] == ["Waiting"]
    assert rows[0]["id"] == waiting


def test_awaiting_respects_min_age_and_limit(db):
    from hunter.tracker import get_rows_awaiting_outcome

    old = (date.today() - timedelta(days=30)).isoformat()
    fresh = date.today().isoformat()
    _insert(db, sent=old, company="Old1")
    _insert(db, sent=old, company="Old2")
    _insert(db, sent=fresh, company="Fresh")

    aged = get_rows_awaiting_outcome(limit=10, min_age_days=14)
    assert {r["company"] for r in aged} == {"Old1", "Old2"}

    assert len(get_rows_awaiting_outcome(limit=1)) == 1


# ── funnel ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("label", ["interview", "rejected", "offer"])
def test_a_reply_label_counts_as_answered(label):
    assert funnel._is_answered("", label) is True


def test_silence_is_recorded_but_not_answered():
    assert funnel._is_answered("", "silence") is False
    assert funnel._has_outcome("silence") is True


def test_legacy_answer_text_still_counts():
    assert funnel._is_answered("got a call from HR", "") is True


def test_nothing_recorded_is_neither_answered_nor_an_outcome():
    assert funnel._is_answered("", "") is False
    assert funnel._has_outcome("") is False


def test_compute_funnel_separates_no_replies_from_no_data(db):
    """The whole feature in one assertion: answered == 0 means something only
    when outcome_recorded > 0."""
    _insert(db, sent="2026-09-01")  # sent, nothing recorded
    _insert(db, sent="2026-09-01", label="silence")  # observed: no reply
    _insert(db, sent="2026-09-01", label="interview")  # a reply

    rep = funnel.compute_funnel()
    assert rep.overall.sent == 3
    assert rep.overall.outcome_recorded == 2
    assert rep.overall.answered == 1


def test_compute_funnel_tolerates_a_db_without_the_outcome_column(tmp_path, monkeypatch):
    """get_db() does not migrate; /funnel must not crash on an older database."""
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE applications (id TEXT, date TEXT, ats_status TEXT, url TEXT, "
        "sent TEXT, confirmation TEXT, answer TEXT)"
    )
    conn.execute(
        "INSERT INTO applications VALUES ('a1b2c3d4', ?, '80%', 'https://x.com/1', '2026-09-01', '', '')",
        (date.today().isoformat(),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(funnel, "DB_PATH", path)

    rep = funnel.compute_funnel()
    assert rep.overall.sent == 1
    assert rep.overall.outcome_recorded == 0


# ── /outcome callback wiring ─────────────────────────────────────────────────


def test_callback_data_round_trips_and_fits_telegram_limit():
    from hunter.commands.outcome import callback_data, parse_callback_data
    from hunter.tracker import OUTCOME_LABELS

    for label in OUTCOME_LABELS:
        data = callback_data("a1b2c3d4", label)
        assert len(data.encode("utf-8")) <= 64
        assert parse_callback_data(data) == ("a1b2c3d4", label)


@pytest.mark.parametrize(
    "data",
    ["", "apply:abc", "outcome:a1b2c3d4", "outcome:a1b2c3d4:ghosted", "skip:a1b2c3d4:offer"],
)
def test_parse_callback_data_ignores_anything_that_is_not_ours(data):
    from hunter.commands.outcome import parse_callback_data

    assert parse_callback_data(data) is None


def test_outcome_callback_is_registered_before_the_catch_all_button_handler():
    """button_callback has no pattern and catches every callback query. If the
    outcome handler came after it, a press would be read as an Apply/Skip action
    on an unknown job and answered "Expired" without recording anything."""
    from pathlib import Path

    src = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("hunter", "telegram_bot.py")
        .read_text(encoding="utf-8")
    )
    outcome_at = src.index("require_user(outcome_callback)")
    catch_all_at = src.index("require_owner(button_callback)")
    assert outcome_at < catch_all_at
