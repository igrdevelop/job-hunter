"""Workable job_fetch text building (no network for API; path parsing only)."""

from job_fetch.ats_workable import _parse_workable_path, _workable_dict_to_text


def test_parse_path_short_form() -> None:
    assert _parse_workable_path("/j/ABC123def", "") == (None, "ABC123def")


def test_parse_path_with_account() -> None:
    assert _parse_workable_path("/netguru/j/02D3AC5276", "") == ("netguru", "02D3AC5276")


def test_dict_to_text_combines_sections() -> None:
    data = {
        "title": "Engineer",
        "remote": True,
        "location": {"city": "Wrocław", "country": "Poland"},
        "description": "<p>Do <strong>things</strong>.</p>",
        "requirements": "Python",
        "benefits": None,
    }
    text = _workable_dict_to_text(data)
    assert "Engineer" in text
    assert "Python" in text
    assert "things" in text
    assert "Remote" in text or "Wrocław" in text
