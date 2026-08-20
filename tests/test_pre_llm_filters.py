"""Tests for pre-LLM filter improvements:

1. expired_check — new patterns (Polish not-found, generic 404)
2. filters     — _is_unacceptable_contract, _requires_relocation,
                  extra_anti_hybrid_cities from config
3. apply_shared — is_react_only_job_text, is_backend_only_job_text
"""

from __future__ import annotations

from unittest.mock import patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# 1. expired_check — new patterns
# ─────────────────────────────────────────────────────────────────────────────

from hunter.expired_check import is_job_expired, is_expired_by_html


class TestNewExpiredPatterns:
    # Polish NoFluffJobs "not available" variants
    def test_ta_oferta_nie_jest_juz_dostepna(self):
        assert is_job_expired("Ta oferta nie jest już dostępna")

    def test_oferta_pracy_nie_zostala_odnaleziona(self):
        assert is_job_expired("Oferta pracy nie została odnaleziona")

    def test_oferta_nie_jest_juz_dostepna_variant(self):
        assert is_job_expired("Oferta nie jest już dostępna")

    # Generic 404 / "we couldn't find"
    def test_we_didnt_find_page(self):
        assert is_job_expired("We didn't find the page you were looking for.")

    def test_we_didnt_find_job(self):
        assert is_job_expired("We didn't find this job in our system.")

    def test_we_couldnt_find_position(self):
        assert is_job_expired("We couldn't find this position.")

    def test_page_not_found(self):
        assert is_job_expired("Page not found — 404")

    def test_404_not_found(self):
        assert is_job_expired("404 Not Found")

    # SmartRecruiters inactive form
    def test_smartrecruiters_inactive_form(self):
        assert is_job_expired("Hey, requested application form is inactive")

    # Generic ATS job closed
    def test_this_job_is_closed(self):
        assert is_job_expired("This job has been closed")

    def test_this_position_is_closed(self):
        assert is_job_expired("This position is now closed")

    def test_sorry_no_longer_available(self):
        assert is_job_expired("Sorry, this job is no longer available")

    # Existing patterns still pass
    def test_existing_offer_expired(self):
        assert is_job_expired("Offer expired")

    def test_existing_no_longer_accepting(self):
        assert is_job_expired("No longer accepting applications")

    def test_existing_pracuj_pl(self):
        assert is_job_expired("Pracodawca zakończył zbieranie zgłoszeń na tę ofertę")

    # False-positive guard — normal job text should NOT trigger
    def test_normal_job_text_not_expired(self):
        assert not is_job_expired(
            "We are looking for an Angular developer. "
            "We didn't find a better fit for our team."  # "we didn't find" inside context
        )

    def test_blank_text_not_expired(self):
        assert not is_job_expired("")

    def test_none_text_not_expired(self):
        assert not is_job_expired(None)  # type: ignore[arg-type]


class TestHtmlMarkersNoFluffJobs:
    def test_nofluffjobs_ta_oferta(self):
        assert is_expired_by_html("<div>ta oferta nie jest już dostępna</div>", "nofluffjobs.com")

    def test_nofluffjobs_oferta_pracy(self):
        assert is_expired_by_html("oferta pracy nie została odnaleziona", "nofluffjobs.com")

    def test_nofluffjobs_existing_marker(self):
        assert is_expired_by_html("This offer is no longer available", "nofluffjobs.com")

    def test_different_domain_not_triggered(self):
        # nofluffjobs marker should NOT fire for pracuj.pl domain
        assert not is_expired_by_html("ta oferta nie jest już dostępna", "pracuj.pl")


# ─────────────────────────────────────────────────────────────────────────────
# 2. filters — contract + relocation + extra anti-hybrid cities
# ─────────────────────────────────────────────────────────────────────────────

from hunter.models import Job
from hunter.filters import (
    _ANTI_HYBRID_CITIES,
    apply_filters_with_stats,
)


def _make_job(**kwargs) -> Job:
    defaults = {
        "title": "Senior Angular Developer",
        "company": "Acme",
        "location": "remote",
        "salary": None,
        "url": "https://example.com/job/1",
        "source": "test",
        "raw": {},
    }
    defaults.update(kwargs)
    return Job(**defaults)


# ── Contract filter ──────────────────────────────────────────────────────────


class TestIsUnacceptableContract:
    def test_part_time_in_description(self):
        job = _make_job(raw={"description": "This is a part-time position, 20h/week."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_unacceptable_contract": True}
        ):
            from hunter.filters import _is_unacceptable_contract as fn

            assert fn(job)

    def test_pol_etatu_polish(self):
        job = _make_job(raw={"description": "Wymiar pracy: pół etatu (0,5 FTE)."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_unacceptable_contract": True}
        ):
            from hunter.filters import _is_unacceptable_contract as fn

            assert fn(job)

    def test_one_month_contract(self):
        job = _make_job(raw={"description": "This is a 1-month contract assignment."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_unacceptable_contract": True}
        ):
            from hunter.filters import _is_unacceptable_contract as fn

            assert fn(job)

    def test_full_time_not_blocked(self):
        job = _make_job(raw={"description": "Full-time permanent position. 40h/week."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_unacceptable_contract": True}
        ):
            from hunter.filters import _is_unacceptable_contract as fn

            assert not fn(job)

    def test_disabled_by_config(self):
        job = _make_job(raw={"description": "This is a part-time role."})
        with patch(
            "hunter.filters.FILTER",
            {**_make_filter_patch(), "exclude_unacceptable_contract": False},
        ):
            from hunter.filters import _is_unacceptable_contract as fn

            assert not fn(job)


# ── Relocation filter ────────────────────────────────────────────────────────


class TestRequiresRelocation:
    def test_relocation_required(self):
        job = _make_job(raw={"description": "Relocation is required to our Warsaw office."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_relocation_required": True}
        ):
            from hunter.filters import _requires_relocation as fn

            assert fn(job)

    def test_must_relocate(self):
        job = _make_job(raw={"description": "Candidates must be willing to relocate."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_relocation_required": True}
        ):
            from hunter.filters import _requires_relocation as fn

            assert fn(job)

    def test_relokacja_wymagana_polish(self):
        job = _make_job(raw={"description": "Relokacja jest wymagana do biura w Helsinkach."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_relocation_required": True}
        ):
            from hunter.filters import _requires_relocation as fn

            assert fn(job)

    def test_relocation_package_not_blocked(self):
        # "relocation package provided" should NOT trigger
        job = _make_job(raw={"description": "We offer a generous relocation package."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_relocation_required": True}
        ):
            from hunter.filters import _requires_relocation as fn

            assert not fn(job)

    def test_disabled_by_config(self):
        job = _make_job(raw={"description": "Relocation is required."})
        with patch(
            "hunter.filters.FILTER", {**_make_filter_patch(), "exclude_relocation_required": False}
        ):
            from hunter.filters import _requires_relocation as fn

            assert not fn(job)


# ── Extra anti-hybrid cities merged from config ──────────────────────────────


class TestExtraAntiHybridCities:
    def test_helsinki_in_set(self):
        assert "helsinki" in _ANTI_HYBRID_CITIES

    def test_barcelona_in_set(self):
        assert "barcelona" in _ANTI_HYBRID_CITIES

    def test_islamabad_in_set(self):
        assert "islamabad" in _ANTI_HYBRID_CITIES

    def test_existing_krakow_still_in_set(self):
        assert "krakow" in _ANTI_HYBRID_CITIES

    def test_location_helsinki_blocked(self):
        job = _make_job(location="Helsinki, Finland")
        _, reasons = apply_filters_with_stats([job])
        assert reasons["location"] == 1

    def test_location_barcelona_blocked(self):
        job = _make_job(location="Barcelona, Spain")
        _, reasons = apply_filters_with_stats([job])
        assert reasons["location"] == 1


# ── reason_counts includes new keys ──────────────────────────────────────────


class TestReasonCountsKeys:
    def test_new_keys_present(self):
        _, reasons = apply_filters_with_stats([])
        assert "contract" in reasons
        assert "relocation" in reasons

    def test_contract_counted(self):
        job = _make_job(raw={"description": "This is a part-time role, 20h/week."})
        _, reasons = apply_filters_with_stats([job])
        assert reasons["contract"] == 1

    def test_relocation_counted(self):
        job = _make_job(raw={"description": "Relocation is required to our office."})
        _, reasons = apply_filters_with_stats([job])
        assert reasons["relocation"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# 3. apply_shared — is_react_only_job_text / is_backend_only_job_text
# ─────────────────────────────────────────────────────────────────────────────

from hunter.apply_shared import is_react_only_job_text, is_backend_only_job_text


class TestIsReactOnlyJobText:
    def test_react_three_times_no_angular(self):
        text = "We need a React developer. React experience required. Build React components."
        assert is_react_only_job_text(text)

    def test_react_with_angular_passes(self):
        text = "We use Angular and React. React experience is a plus."
        assert not is_react_only_job_text(text)

    def test_react_twice_not_enough(self):
        text = "Experience with React is nice. React knowledge helpful."
        assert not is_react_only_job_text(text)

    def test_no_react_passes(self):
        text = "Looking for an Angular developer with TypeScript skills."
        assert not is_react_only_job_text(text)

    def test_react_native_counted(self):
        # React Native also contains "react" word
        text = "React Native developer. React Native apps. React Native UI."
        assert is_react_only_job_text(text)

    def test_case_insensitive(self):
        text = "REACT developer. REACT skills required. Building REACT apps."
        assert is_react_only_job_text(text)

    def test_empty_text(self):
        assert not is_react_only_job_text("")


class TestIsBackendOnlyJobText:
    def test_python_required_no_fe(self):
        text = (
            "We need a Python developer. Python is required. "
            "You will build backend APIs using Django. "
            "Must have strong Python skills."
        )
        assert is_backend_only_job_text(text)

    def test_php_must_have_no_fe(self):
        text = "PHP developer needed. PHP is a must-have. Building Laravel applications."
        assert is_backend_only_job_text(text)

    def test_angular_present_passes(self):
        text = "Python required for backend. Frontend in Angular. Must have Python skills."
        assert not is_backend_only_job_text(text)

    def test_react_present_passes(self):
        text = "Python required. React for frontend. Must have Python experience."
        assert not is_backend_only_job_text(text)

    def test_python_as_bonus_passes(self):
        # Python mentioned but no hard-requirement qualifier
        text = (
            "Angular developer. Knowledge of Python would be a bonus. Angular experience required."
        )
        assert not is_backend_only_job_text(text)

    def test_no_be_lang_passes(self):
        text = "Frontend developer. Must have TypeScript skills. Angular required."
        assert not is_backend_only_job_text(text)

    def test_java_excluded_javascript_not(self):
        # "java" in the regex should NOT match "javascript"
        text = "JavaScript developer. Must have JavaScript skills. We require JavaScript."
        assert not is_backend_only_job_text(text)

    def test_empty_text(self):
        assert not is_backend_only_job_text("")


class TestBackendOnlySkipWritesTrackerRow:
    """Wiring test for apply_api.py Step 1.5d: a backend-only pre-LLM skip must
    write a terminal SKIP row, exactly like the React-only skip and the doomed
    gate do — otherwise the pending placeholder is cleared with nothing to
    dedup against, and the same URL is re-fetched/re-queued/re-notified on
    every future hunt cycle (observed live: the same posting skipped 8 times
    over ~40h before this was fixed)."""

    def test_backend_only_skip_calls_add_skipped(self, monkeypatch) -> None:
        from hunter.apply_api import main_api

        monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
        text = (
            "We need a Python developer. Python is required. "
            "You will build backend APIs using Django. "
            "Must have strong Python skills. " * 5
        )
        with (
            patch("hunter.sources.fetch_job_text", return_value=text),
            patch("hunter.apply_api.notify"),
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
        ):
            main_api(
                "https://example.com/backend-only",
                jobleads_company="Acme",
                jobleads_title="Staff Python Developer",
            )

        mock_add_skipped.assert_called_once()
        job_arg = mock_add_skipped.call_args[0][0]
        assert job_arg.url == "https://example.com/backend-only"
        assert job_arg.company == "Acme"
        assert job_arg.title == "Staff Python Developer"

    def test_tracker_write_failure_does_not_raise(self, monkeypatch) -> None:
        """Best-effort: a DB error while writing the SKIP row must not blow up
        the apply run — main_api should still return normally."""
        from hunter.apply_api import main_api

        monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
        text = "PHP developer needed. PHP is a must-have. Building Laravel applications. " * 5
        with (
            patch("hunter.sources.fetch_job_text", return_value=text),
            patch("hunter.apply_api.notify"),
            patch("hunter.tracker.add_skipped", side_effect=RuntimeError("db locked")),
        ):
            main_api("https://example.com/backend-only-2")  # must not raise


class TestCompanyTitleDedupGate:
    """Step 4.55 (apply_api.py): manual entry points (URL paste, LinkedIn batch,
    forwarded text) call main_api directly with only a URL — they never run the
    hunt loop's own dedup_key check (hunter/main.py Step 3) before spending on
    generation. A vacancy re-listed under a new URL (same company+role, e.g.
    the 2026-08-20 Comarch incident: "Comarch SA" via pracuj.pl vs plain
    "Comarch" via LinkedIn, three days apart) must be caught here instead,
    once the LLM has extracted company_name/job_title — before the expensive
    ATS-loop/judge/verdict machinery runs."""

    def _golden_content(self) -> dict:
        import json
        from pathlib import Path

        fixture = Path(__file__).parent / "fixtures" / "golden" / "generation_response.json"
        return json.loads(fixture.read_text(encoding="utf-8"))

    def test_known_company_title_skips_before_ats_loop(self, monkeypatch) -> None:
        from hunter.apply_api import main_api
        from hunter.tracker import dedup_key

        content = self._golden_content()
        known_key = dedup_key(content["company_name"], content["job_title"])

        monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
        with (
            patch(
                "hunter.sources.fetch_job_text",
                return_value="Angular developer role. " * 50,
            ),
            patch("llm_client.call_llm", return_value=content),
            patch("hunter.apply_api.notify"),
            patch("hunter.tracker.get_known_company_titles", return_value={known_key}),
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
            patch("hunter.apply_api._ats_check_loop") as mock_ats_loop,
        ):
            main_api("https://example.com/comarch-repost")

        mock_ats_loop.assert_not_called()
        mock_add_skipped.assert_called_once()
        job_arg = mock_add_skipped.call_args[0][0]
        assert job_arg.url == "https://example.com/comarch-repost"
        assert job_arg.company == content["company_name"]
        assert job_arg.title == content["job_title"]

    def test_unknown_company_title_proceeds_to_ats_loop(self, monkeypatch) -> None:
        """A sentinel exception raised from _ats_check_loop stops the test right
        as the gate hands off — proves the gate did NOT skip, without running
        the rest of the pipeline (doc rendering needs a configured candidate.yaml
        this dev worktree doesn't have)."""
        from hunter.apply_api import main_api

        content = self._golden_content()

        class _ReachedAtsLoop(Exception):
            pass

        monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
        with (
            patch(
                "hunter.sources.fetch_job_text",
                return_value="Angular developer role. " * 50,
            ),
            patch("llm_client.call_llm", return_value=content),
            patch("hunter.apply_api.notify"),
            patch("hunter.tracker.get_known_company_titles", return_value=set()),
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
            patch("hunter.apply_api._ats_check_loop", side_effect=_ReachedAtsLoop),
            pytest.raises(_ReachedAtsLoop),
        ):
            main_api("https://example.com/comarch-new-role")

        mock_add_skipped.assert_not_called()

    def test_force_mode_bypasses_dedup_gate(self, monkeypatch) -> None:
        """`/force` (skip_dedup=True) must reach the ATS loop even when the
        company+title is already known — same bypass semantics as every other
        dedup gate in this pipeline."""
        from hunter.apply_api import main_api

        content = self._golden_content()

        class _ReachedAtsLoop(Exception):
            pass

        monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
        with (
            patch(
                "hunter.sources.fetch_job_text",
                return_value="Angular developer role. " * 50,
            ),
            patch("llm_client.call_llm", return_value=content),
            patch("hunter.apply_api.notify"),
            patch("hunter.tracker.get_known_company_titles", return_value={"anything"}),
            patch("hunter.tracker.dedup_key", return_value="anything"),
            patch("hunter.tracker.add_skipped") as mock_add_skipped,
            patch("hunter.apply_api._ats_check_loop", side_effect=_ReachedAtsLoop),
            pytest.raises(_ReachedAtsLoop),
        ):
            main_api("https://example.com/comarch-force", skip_dedup=True)

        mock_add_skipped.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_filter_patch() -> dict:
    """Minimal FILTER dict so patching works without breaking other checks."""
    from hunter.config import FILTER

    return dict(FILTER)
