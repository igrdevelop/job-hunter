"""M1 tests: pure logic for the LinkedIn posts scout (no Playwright/browser).

See docs/LINKEDIN_POSTS_SCOUT_TASK.md for the milestone spec.
"""

from pathlib import Path

import pytest

from linkedin_scout.heuristics import (
    LocationVerdict,
    check_location,
    is_hiring_post,
)
from linkedin_scout.parser import parse_posts
from linkedin_scout.seen_store import SeenStore, dedup_key

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "linkedin_scout"


def _read_fixture(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# --- is_hiring_post -------------------------------------------------------


def test_genuine_en_hiring_post_passes():
    text = (
        "We're hiring! Join our team as an Angular Developer working with "
        "Java, React and Angular for our banking client. Fully remote."
    )
    assert is_hiring_post(text) is True


def test_genuine_pl_hiring_post_passes():
    text = (
        "Szukamy osoby od frontendu na rolę Senior Frontend Engineer "
        "(Angular). Praca zdalna z całej Polski, umowa B2B."
    )
    assert is_hiring_post(text) is True


def test_candidate_side_singular_szukam_rejected():
    text = (
        "Szukam nowego projektu jako Frontend Developer (Angular). "
        "Otwarta na oferty B2B, zdalnie."
    )
    assert is_hiring_post(text) is False


def test_szukam_does_not_match_inside_szukamy():
    """The singular/plural distinction is load-bearing (plan §4.6 round 2)."""
    hiring_text = "Szukamy Angular developera do zespołu, praca zdalna."
    assert is_hiring_post(hiring_text) is True


def test_open_to_work_rejected():
    text = "Open to work! Angular developer looking for new opportunities."
    assert is_hiring_post(text) is False


def test_course_spam_rejected():
    text = (
        "Free webinar: Learn Angular in 30 days! Join our bootcamp and "
        "become a frontend developer. #hiring"
    )
    assert is_hiring_post(text) is False


def test_us_staffing_w2_onsite_rejected():
    text = (
        "Hiring NOW: Full Stack Developer (Java, .NET, Angular, React) - "
        "W2 only, on-site in Richmond, VA. C2C not accepted. USC/GC required."
    )
    assert is_hiring_post(text) is False


def test_bare_stack_dump_mention_without_prominence_rejected():
    filler = "x" * 250
    text = (
        f"{filler} We are hiring a backend engineer with experience in "
        "Java, Spring, Kafka, Docker, and some Angular for the admin panel."
    )
    assert is_hiring_post(text) is False


def test_angular_role_phrase_counts_as_prominent_even_late_in_text():
    filler = "Great company culture and benefits. " * 10
    text = f"{filler} We're hiring an Angular Developer to join our team."
    assert is_hiring_post(text) is True


def test_no_stack_keyword_rejected():
    text = "We're hiring! Join our team as a React Developer."
    assert is_hiring_post(text) is False


def test_empty_text_rejected():
    assert is_hiring_post("") is False


# --- check_location ---------------------------------------------------------


def test_location_explicit_remote_keeps():
    text = _read_fixture("hiring_post_no_location.txt").replace(
        "growing team.", "growing team, fully remote."
    )
    assert check_location(text) is LocationVerdict.KEEP


def test_location_explicit_wroclaw_keeps():
    text = "We're hiring an Angular Developer, hybrid from our Wrocław office."
    assert check_location(text) is LocationVerdict.KEEP


def test_location_onsite_other_polish_city_rejects():
    text = _read_fixture("hiring_post_onsite_other_city.txt")
    assert check_location(text) is LocationVerdict.REJECT


def test_location_unknown_keeps():
    text = _read_fixture("hiring_post_no_location.txt")
    assert check_location(text) is LocationVerdict.KEEP


def test_location_empty_text_keeps():
    assert check_location("") is LocationVerdict.KEEP


# --- parser.parse_posts -----------------------------------------------------


def test_parse_posts_splits_feed_sample_into_five_blocks():
    text = _read_fixture("feed_sample.txt")
    posts = parse_posts(text)
    assert len(posts) == 5


def test_parse_posts_extracts_author_and_strips_header_noise():
    text = _read_fixture("feed_sample.txt")
    posts = parse_posts(text)
    deloitte = posts[0]
    assert deloitte.author == "Deloitte Poland"
    # header noise (3rd+, title line, timestamp, Follow) must be gone
    assert "3rd+" not in deloitte.body
    assert "Follow" not in deloitte.body
    assert "Talent Acquisition Specialist" not in deloitte.body
    assert deloitte.body.startswith("We're hiring!")


def test_parse_posts_second_block_uses_connect_as_header_end():
    text = _read_fixture("feed_sample.txt")
    posts = parse_posts(text)
    recruiter = posts[1]
    assert recruiter.author == "John Smith"
    assert "Connect" not in recruiter.body
    assert recruiter.body.startswith("Hiring NOW:")


def test_parse_posts_full_pipeline_matches_expected_hiring_posts():
    text = _read_fixture("feed_sample.txt")
    posts = parse_posts(text)
    hiring = [p for p in posts if is_hiring_post(p.body)]
    authors = {p.author for p in hiring}
    assert authors == {"Deloitte Poland", "Piotr Nowak"}


def test_parse_posts_no_marker_returns_empty():
    assert parse_posts("just some random text with no markers") == []


def test_parse_posts_empty_text_returns_empty():
    assert parse_posts("") == []


def test_parse_posts_skips_marker_with_no_author():
    text = "Feed post\n\n\nFeed post\n\nReal Author\nFollow\nActual body text here."
    posts = parse_posts(text)
    assert len(posts) == 1
    assert posts[0].author == "Real Author"


# --- seen_store --------------------------------------------------------------


def test_dedup_key_is_stable_for_same_input():
    key1 = dedup_key("Jane Doe", "We're hiring an Angular developer")
    key2 = dedup_key("Jane Doe", "We're hiring an Angular developer")
    assert key1 == key2


def test_dedup_key_differs_for_different_author():
    key1 = dedup_key("Jane Doe", "Same text")
    key2 = dedup_key("John Doe", "Same text")
    assert key1 != key2


def test_dedup_key_only_uses_first_200_chars_of_text():
    long_text = "a" * 300
    key1 = dedup_key("Author", long_text[:200] + "TAIL_ONE")
    key2 = dedup_key("Author", long_text[:200] + "TAIL_TWO")
    assert key1 == key2


def test_seen_store_roundtrip(tmp_path):
    path = tmp_path / "seen_posts.json"
    store = SeenStore(path)
    key = dedup_key("Author", "text")
    assert store.is_seen(key) is False
    store.mark_seen(key)
    store.save()

    reloaded = SeenStore(path)
    assert reloaded.is_seen(key) is True


def test_seen_store_missing_file_starts_empty(tmp_path):
    store = SeenStore(tmp_path / "does_not_exist.json")
    assert store.load() == set()


def test_seen_store_corrupt_file_treated_as_empty(tmp_path):
    path = tmp_path / "seen_posts.json"
    path.write_text("{ not valid json", encoding="utf-8")
    store = SeenStore(path)
    assert store.load() == set()


def test_seen_store_save_is_atomic_no_leftover_tmp(tmp_path):
    path = tmp_path / "seen_posts.json"
    store = SeenStore(path)
    store.mark_seen("abc")
    store.save()
    assert path.exists()
    assert not (tmp_path / "seen_posts.json.tmp").exists()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
