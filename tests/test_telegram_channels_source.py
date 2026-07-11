"""Tests for hunter/sources/telegram_channels.py (M1: parser + prefilter +
Source skeleton; M2 adds fetch_text/matches_url/registration coverage).

Fixtures under tests/fixtures/telegram_channels/ are trimmed real captures of
t.me/s/{channel} and t.me/{channel}/{id}?embed=1&mode=tme pages — see the
comment at the top of each fixture file for provenance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hunter.sources.telegram_channels import (
    TelegramChannelsSource,
    TgPost,
    build_job,
    guess_company,
    guess_location,
    passes_prefilter,
    synthesize_title,
    _parse_permalink,
    _parse_posts,
)

FIXTURES = Path(__file__).parent / "fixtures" / "telegram_channels"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


# ── Parser: post splitting, permalink, links, br-handling, media-only ───────


def test_parse_posts_splits_digest_channel_into_three_posts():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    assert len(posts) == 3
    assert [p.msg_id for p in posts] == [894, 901, 896]
    assert all(p.has_text for p in posts)


def test_parse_posts_permalink_and_channel():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    assert posts[0].permalink == "https://t.me/findmyremote_frontend/894"
    assert posts[0].channel == "findmyremote_frontend"


def test_parse_posts_br_converted_to_newlines():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    text = posts[0].text
    assert "\n" in text
    first_line = text.split("\n")[0].strip()
    assert first_line == "Hey job seekers! Check out a handful of remote front-end roles (13 found)!"


def test_parse_posts_extracts_first_external_link():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    assert posts[0].links[0] == (
        "https://findmyremote.ai/companies/jobgether-1/jobs/"
        "team-lead-frontend-game-development-pixi-js-186672553"
    )


def test_parse_posts_media_only_has_no_text():
    posts = _parse_posts(_load("channel_media_only.html"), "Remoteit")
    assert len(posts) == 1
    assert posts[0].has_text is False
    assert posts[0].text == ""
    assert posts[0].links == []


def test_parse_posts_drops_photo_wrapper_and_hashtag_links():
    posts = _parse_posts(_load("channel_board_mixed.html"), "IT_job_Poland")
    angular_post = next(p for p in posts if p.msg_id == 1373)
    # telegra.ph/file/... (photo caption wrapper) and "?q=#tag" (relative
    # hashtag search links) must both be dropped — only the real apply link
    # survives.
    assert angular_post.links == ["https://wroctech.example.com/careers/senior-frontend-angular"]


def test_parse_posts_emoji_survive_in_text():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    assert "🇪🇸" in posts[0].text or "Spain" in posts[0].text


# ── Prefilter (§2.5) ─────────────────────────────────────────────────────────


def test_prefilter_positive_ru_hiring_board_post():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    hiring_post = next(p for p in posts if p.msg_id == 449)
    assert passes_prefilter(hiring_post.text, kind="board") is True


def test_prefilter_negative_candidate_side_ru():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    candidate_post = next(p for p in posts if p.msg_id == 460)
    assert passes_prefilter(candidate_post.text, kind="board") is False


def test_prefilter_negative_mentor_spam_ru():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    spam_post = next(p for p in posts if p.msg_id == 447)
    assert passes_prefilter(spam_post.text, kind="board") is False


def test_prefilter_negative_interview_practice_event_ru():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    event_post = next(p for p in posts if p.msg_id == 452)
    assert passes_prefilter(event_post.text, kind="board") is False


def test_prefilter_negative_no_title_keyword():
    assert passes_prefilter("Just a generic update with no relevant tech at all.", kind="board") is False


def test_prefilter_negative_exclude_pattern_dotnet():
    posts = _parse_posts(_load("channel_board_mixed.html"), "IT_job_Poland")
    dotnet_post = next(p for p in posts if p.msg_id == 1380)
    assert passes_prefilter(dotnet_post.text, kind="board") is False


def test_prefilter_board_kind_skips_hiring_signal_requirement():
    text = "Senior Angular Developer | Remote | apply at example.com"
    assert passes_prefilter(text, kind="board") is True
    assert passes_prefilter(text, kind="authored") is False


def test_prefilter_authored_kind_requires_hiring_signal():
    text = "We're hiring a Senior Frontend (Angular) developer, apply now."
    assert passes_prefilter(text, kind="authored") is True


def test_prefilter_empty_text_rejected():
    assert passes_prefilter("", kind="board") is False
    assert passes_prefilter("   ", kind="board") is False


# ── Title synthesis (§2.4) ───────────────────────────────────────────────────


def test_synthesize_title_keyword_already_in_first_line():
    text = "Senior Frontend Engineer (Angular) role just opened up!\n\nmore body text"
    title = synthesize_title(text, matched_kw="angular")
    assert title == "Senior Frontend Engineer (Angular) role just opened up!"


def test_synthesize_title_appends_matched_keyword_when_absent():
    text = "Some generic recruiter blurb about a great opportunity!\n\nWe need an Angular expert."
    title = synthesize_title(text, matched_kw="angular")
    assert title == "Some generic recruiter blurb about a great opportunity! · angular"


def test_synthesize_title_caps_at_90_chars():
    text = "A" * 150
    title = synthesize_title(text, matched_kw=None)
    assert len(title) == 90
    assert title == "A" * 90


def test_synthesize_title_empty_text_falls_back():
    assert synthesize_title("", matched_kw=None) == "Telegram post"


# ── Job assembly (§2.1) ──────────────────────────────────────────────────────


def test_build_job_prefers_external_link_cleaned():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    job = build_job(posts[0], kind="board", source_name="telegram_channels")
    assert job.url == (
        "https://findmyremote.ai/companies/jobgether-1/jobs/"
        "team-lead-frontend-game-development-pixi-js-186672553"
    )
    assert job.source == "telegram_channels"


def test_build_job_falls_back_to_permalink_when_no_external_link():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    no_link_post = next(p for p in posts if p.msg_id == 447)
    job = build_job(no_link_post, kind="board", source_name="telegram_channels")
    assert job.url == "https://t.me/rabotafrontend/447"


def test_build_job_raw_carries_permalink_both_keys():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    job = build_job(posts[0], kind="board", source_name="telegram_channels")
    assert job.raw["permalink"] == "https://t.me/findmyremote_frontend/894"
    assert job.raw["tg_permalink"] == "https://t.me/findmyremote_frontend/894"
    assert "post_text" not in job.raw  # must never trigger the scout-relay paste flow


def test_build_job_location_remote_detected():
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    hiring_post = next(p for p in posts if p.msg_id == 449)
    job = build_job(hiring_post, kind="board", source_name="telegram_channels")
    assert job.location == "Remote"


def test_build_job_location_empty_when_no_remote_token():
    job = build_job(
        TgPost(1, "chan", "https://t.me/chan/1", "Angular Developer needed, office-based.", []),
        kind="board",
        source_name="telegram_channels",
    )
    assert job.location == ""


def test_guess_company_from_at_pattern():
    posts = _parse_posts(_load("channel_board_findmyremote.html"), "findmyremote_frontend")
    assert guess_company(posts[0].text, "findmyremote_frontend") == "Jobgether"


def test_guess_company_falls_back_to_channel_name():
    assert guess_company("no at-pattern here at all", "somechannel") == "@somechannel"


def test_guess_location_remote_ru_token():
    assert guess_location("Формат: удаленно") == "Remote"


def test_guess_location_empty_without_token():
    assert guess_location("Office in Warsaw, on-site only.") == ""


# ── fetch_text (embed page) ──────────────────────────────────────────────────


def test_fetch_text_returns_post_body():
    source = TelegramChannelsSource()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.raw.read.return_value = _load("embed_post.html").encode("utf-8")
    with patch("hunter.sources.telegram_channels.requests.get", return_value=resp) as mock_get:
        text = source.fetch_text("https://t.me/rabotafrontend/449")
    assert "uInflow" in text
    mock_get.assert_called_once()
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://t.me/rabotafrontend/449?embed=1&mode=tme"


def test_fetch_text_raises_on_deleted_post():
    source = TelegramChannelsSource()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.raw.read.return_value = _load("embed_deleted.html").encode("utf-8")
    with patch("hunter.sources.telegram_channels.requests.get", return_value=resp):
        with pytest.raises(ValueError):
            source.fetch_text("https://t.me/rabotafrontend/1")


def test_fetch_text_handles_s_prefixed_url():
    source = TelegramChannelsSource()
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.raw.read.return_value = _load("embed_post.html").encode("utf-8")
    with patch("hunter.sources.telegram_channels.requests.get", return_value=resp) as mock_get:
        source.fetch_text("https://t.me/s/rabotafrontend/449")
    called_url = mock_get.call_args[0][0]
    assert called_url == "https://t.me/rabotafrontend/449?embed=1&mode=tme"


# ── matches_url ───────────────────────────────────────────────────────────────


def test_matches_url_t_me_forms():
    source = TelegramChannelsSource()
    assert source.matches_url("https://t.me/rabotafrontend/449") is True
    assert source.matches_url("https://t.me/s/rabotafrontend/449") is True
    assert source.matches_url("https://telegram.me/rabotafrontend/449") is True


def test_matches_url_rejects_other_hosts():
    source = TelegramChannelsSource()
    assert source.matches_url("https://example.com/jobs/1") is False
    assert source.matches_url("https://linkedin.com/scout-posts/p123") is False


# ── _parse_permalink ──────────────────────────────────────────────────────────


def test_parse_permalink_plain_form():
    assert _parse_permalink("https://t.me/rabotafrontend/449") == ("rabotafrontend", "449")


def test_parse_permalink_s_prefixed_form():
    assert _parse_permalink("https://t.me/s/rabotafrontend/449") == ("rabotafrontend", "449")


def test_parse_permalink_raises_on_non_post_url():
    with pytest.raises(ValueError):
        _parse_permalink("https://t.me/rabotafrontend")


# ── search() end-to-end (mocked HTTP) ────────────────────────────────────────


def test_search_end_to_end_over_channel_list(tmp_path, monkeypatch):
    channels_file = tmp_path / "telegram_channels.json"
    channels_file.write_text(
        '[{"channel": "findmyremote_frontend", "kind": "board"}]', encoding="utf-8"
    )
    import hunter.config as config
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS_FILE", channels_file)
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS_DELAY_SEC", 0)

    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.raw.read.return_value = _load("channel_board_findmyremote.html").encode("utf-8")
    source = TelegramChannelsSource()
    with patch("hunter.sources.telegram_channels.requests.get", return_value=resp):
        jobs = source.search()

    # Only the Angular post (901) carries the "angular" keyword prominently
    # enough via title_keywords substring match on the whole post text — all
    # three posts mention "front-end"/"frontend" so all should survive the
    # prefilter (none hit exclude/candidate/spam patterns).
    assert len(jobs) == 3
    assert all(j.source == "telegram_channels" for j in jobs)
    assert all(j.url for j in jobs)


def test_search_returns_empty_and_does_not_crash_on_fetch_error(tmp_path, monkeypatch):
    channels_file = tmp_path / "telegram_channels.json"
    channels_file.write_text(
        '[{"channel": "brokenchannel", "kind": "board"}]', encoding="utf-8"
    )
    import hunter.config as config
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS_FILE", channels_file)
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS_DELAY_SEC", 0)

    source = TelegramChannelsSource()
    with patch("hunter.sources.telegram_channels.requests.get", side_effect=OSError("boom")):
        jobs = source.search()
    assert jobs == []


def test_search_missing_channel_list_returns_empty(tmp_path, monkeypatch):
    import hunter.config as config
    monkeypatch.setattr(config, "TELEGRAM_CHANNELS_FILE", tmp_path / "does_not_exist.json")

    source = TelegramChannelsSource()
    assert source.search() == []


# ── M2: registration + dispatch + validation floor ───────────────────────────


def test_registered_in_all_sources_by_default():
    """TELEGRAM_CHANNELS_ENABLED defaults to true, so a normal import (no env
    override) must already include it — same pattern as test_base_source_
    fetch_text.py's direct ALL_SOURCES check."""
    from hunter.sources import ALL_SOURCES
    assert any(s.name == "telegram_channels" for s in ALL_SOURCES)


def test_fetch_roster_includes_telegram_channels():
    from hunter.sources import _fetch_roster
    names = {s.name for s in _fetch_roster()}
    assert "telegram_channels" in names


def test_fetch_job_text_dispatches_t_me_url_to_this_source():
    from hunter.sources import fetch_job_text
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.raw.read.return_value = _load("embed_post.html").encode("utf-8")
    with patch("hunter.sources.telegram_channels.requests.get", return_value=resp):
        text = fetch_job_text("https://t.me/rabotafrontend/449")
    assert "uInflow" in text


def test_fetch_job_text_does_not_claim_non_t_me_external_link():
    """An external-link job's URL (e.g. an ATS/board link) must dispatch to
    the matching board source (or html_fallback), never this one."""
    from hunter.sources import _fetch_roster
    for src in _fetch_roster():
        if src.name == "telegram_channels":
            assert src.matches_url("https://wroctech.example.com/careers/senior-frontend-angular") is False


def test_min_job_text_len_for_t_me_permalink_uses_scout_floor():
    from hunter.validation import MIN_SCOUT_TEXT_LEN, min_job_text_len_for
    assert min_job_text_len_for("https://t.me/rabotafrontend/449") == MIN_SCOUT_TEXT_LEN


def test_min_job_text_len_for_external_link_uses_normal_floor():
    from hunter.validation import MIN_JOB_TEXT_LEN, min_job_text_len_for
    assert min_job_text_len_for("https://wroctech.example.com/careers/x") == MIN_JOB_TEXT_LEN


def test_telegram_post_url_marker_is_substring_of_produced_permalinks():
    """Drift guard: the validation-floor marker must match every permalink
    this source's build_job()/fetch_text() actually produces (same pattern
    as the scout marker test in test_scout_relay_apply_fixes.py)."""
    from hunter.validation import TELEGRAM_POST_URL_MARKER
    posts = _parse_posts(_load("channel_board_rabotafrontend.html"), "rabotafrontend")
    no_link_post = next(p for p in posts if p.msg_id == 447)
    job = build_job(no_link_post, kind="board", source_name="telegram_channels")
    assert TELEGRAM_POST_URL_MARKER in job.url
