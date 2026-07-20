"""
Tests for the same-vacancy re-post gate (hunter/repost_gate.py):

1. Company-name normalization + fuzzy matching (the calibration pairs that
   motivated it: ITDS/ITDS Polska, DHC/DHC Business Solutions, HI Technology
   [And] Innovation, Softgarden [E-]Recruiting — and the agency negatives
   that must NOT match: Hays/LTM, UST/HCLTech, GN Group/Jabra).
2. The decision matrix in find_repost (hard/company/warn/none bands, short
   texts never compared, donor postings below the floor excluded).
3. tracker.get_recent_applied_for_repost (status + window + folder filters)
   and the explicit add_applied(reapplication=True) flag.
4. execute_reuse: copies the donor docs into a fresh dated folder, rewrites
   content.json (new url, $0 cost, reused_from), writes the Re-application
   tracker row, stamps the donor verdict, notifies with files.
5. run_repost_gate orchestration (disabled / force bypass / warn band /
   error tolerance) and the Step 1.5g wiring in the API pipeline.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from hunter.repost_gate import (
    RepostMatch,
    companies_match,
    execute_reuse,
    find_repost,
    normalize_company,
    run_repost_gate,
)

_TODAY = date.today().strftime("%Y-%m-%d")
_LONG_TEXT = (
    "Senior Angular Developer wanted. Requirements: Angular, TypeScript, RxJS, "
    "NgRx, Jest, Cypress, REST APIs, SCSS, Git, Agile. You will build enterprise "
    "SPA applications, own frontend architecture, review code and mentor. "
) * 20  # ~3400 chars — comfortably above MIN_TEXT_CHARS


def _make_donor_folder(
    tmp_path: Path,
    name: str = "AcmeCorp",
    posting: str = _LONG_TEXT,
    verdict: float | None = 91.0,
    with_pdf: bool = True,
) -> Path:
    folder = tmp_path / _TODAY / name
    folder.mkdir(parents=True)
    (folder / "job_posting.txt").write_text(
        f"URL: https://old.example.com/{name}\n\n{posting}", encoding="utf-8"
    )
    content = {
        "company_name": name,
        "job_title": "Senior Angular Developer",
        "stack": "Angular, TypeScript",
        "ats_score": "92",
        "resume_en": {"summary": "Senior Angular dev"},
        "to_learn": "",
    }
    if verdict is not None:
        content["ats_verdict"] = {"score": verdict, "model": "judge"}
    (folder / "content.json").write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
    if with_pdf:
        (folder / f"{name}_CV_EN_ats91.pdf").write_bytes(b"%PDF-fake")
        (folder / "outreach.md").write_text("contact", encoding="utf-8")
    return folder


def _insert_applied_row(url: str, company: str, folder: Path, day: str = _TODAY) -> None:
    from hunter.db import get_db
    from hunter.tracker import DB_PATH, normalize_url

    with get_db(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO applications (id, date, company, title, stack, ats_status,
                                      url, url_norm, folder)
            VALUES (?, ?, ?, 'Senior Angular Developer', 'Angular', '92', ?, ?, ?)
            """,
            (url[-8:], day, company, url, normalize_url(url), str(folder)),
        )


# ── 1. Company normalization + fuzzy matching ────────────────────────────────


class TestCompaniesMatch:
    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("ITDS", "ITDS Polska"),
            ("DHC", "DHC Business Solutions Sp. z o.o."),
            ("HI Technology Innovation", "HI Technology And Innovation"),
            ("Softgarden E-Recruiting", "Softgarden Recruiting"),
            ("Acme", "acme"),
            ("Reply Polska", "reply"),
        ],
    )
    def test_variations_of_the_same_company_match(self, a: str, b: str) -> None:
        assert companies_match(a, b)

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("Hays", "LTM"),
            ("UST", "HCLTech"),
            ("GN Group", "Jabra"),
            ("Comarch", "Hays"),
            ("", "Acme"),
            ("Acme", ""),
            ("Sp. z o.o.", "GmbH"),  # both normalize to empty — never match
        ],
    )
    def test_different_companies_do_not_match(self, a: str, b: str) -> None:
        assert not companies_match(a, b)

    def test_normalize_strips_legal_and_generic_tokens(self) -> None:
        assert normalize_company("DHC Business Solutions Sp. z o.o.") == "dhcbusiness"
        assert normalize_company("ITDS Polska") == "itds"


# ── 2. Decision matrix ───────────────────────────────────────────────────────


class TestFindRepostMatrix:
    def _donors(self, tmp_path: Path, tracker_db, company: str = "AcmeCorp") -> Path:
        folder = _make_donor_folder(tmp_path, company)
        _insert_applied_row(f"https://old.example.com/{company}", company, folder)
        return folder

    def test_identical_text_is_reuse_even_without_company(self, tmp_path, tracker_db) -> None:
        self._donors(tmp_path, tracker_db)
        match = find_repost(_LONG_TEXT, company="", window_days=60)
        assert match is not None
        assert match.action == "reuse"
        assert match.similarity > 0.97

    def test_mid_band_with_matching_company_is_reuse(self, tmp_path, tracker_db, monkeypatch):
        self._donors(tmp_path, tracker_db, company="ITDS")
        monkeypatch.setattr("hunter.repost_gate._similarities", lambda t, d: [0.92])
        match = find_repost(_LONG_TEXT, company="ITDS Polska", window_days=60)
        assert match is not None and match.action == "reuse"

    def test_mid_band_without_company_agreement_is_warn(self, tmp_path, tracker_db, monkeypatch):
        self._donors(tmp_path, tracker_db, company="Hays")
        monkeypatch.setattr("hunter.repost_gate._similarities", lambda t, d: [0.92])
        match = find_repost(_LONG_TEXT, company="LTM", window_days=60)
        assert match is not None and match.action == "warn"

    def test_low_band_is_warn_even_with_matching_company(self, tmp_path, tracker_db, monkeypatch):
        self._donors(tmp_path, tracker_db, company="Acme")
        monkeypatch.setattr("hunter.repost_gate._similarities", lambda t, d: [0.87])
        match = find_repost(_LONG_TEXT, company="Acme", window_days=60)
        assert match is not None and match.action == "warn"

    def test_below_warn_band_is_none(self, tmp_path, tracker_db, monkeypatch) -> None:
        self._donors(tmp_path, tracker_db)
        monkeypatch.setattr("hunter.repost_gate._similarities", lambda t, d: [0.5])
        assert find_repost(_LONG_TEXT, company="AcmeCorp", window_days=60) is None

    def test_short_job_text_never_compared(self, tmp_path, tracker_db) -> None:
        self._donors(tmp_path, tracker_db)
        assert find_repost("short text", company="AcmeCorp", window_days=60) is None

    def test_short_donor_posting_excluded(self, tmp_path, tracker_db) -> None:
        folder = _make_donor_folder(tmp_path, "TinyCo", posting="too short " * 10)
        _insert_applied_row("https://old.example.com/TinyCo", "TinyCo", folder)
        assert find_repost(_LONG_TEXT, company="TinyCo", window_days=60) is None

    def test_donor_folder_missing_on_disk_excluded(self, tmp_path, tracker_db) -> None:
        _insert_applied_row("https://old.example.com/Ghost", "Ghost", tmp_path / "nope" / "Ghost")
        assert find_repost(_LONG_TEXT, company="Ghost", window_days=60) is None


# ── 3. Tracker: donor query + explicit re-application flag ───────────────────


class TestTrackerSupport:
    def test_recent_applied_excludes_non_apply_statuses_and_old_rows(
        self, tmp_path, tracker_db
    ) -> None:
        from hunter.db import get_db
        from hunter.tracker import DB_PATH, get_recent_applied_for_repost

        old_day = (date.today() - timedelta(days=90)).strftime("%Y-%m-%d")
        rows = [
            ("a1", _TODAY, "Live", "92", "Applications/x/Live"),
            ("a2", _TODAY, "Skipped", "SKIP", "Applications/x/Skipped"),
            ("a3", _TODAY, "Failed", "FAIL", "Applications/x/Failed"),
            ("a4", _TODAY, "Expired", "EXPIRED", "Applications/x/Expired"),
            ("a5", _TODAY, "Manual", "MANUAL", "Applications/x/Manual"),
            ("a6", _TODAY, "NoFolder", "92", ""),
            ("a7", old_day, "TooOld", "92", "Applications/x/TooOld"),
        ]
        with get_db(DB_PATH) as conn:
            for rid, day, company, ats, folder in rows:
                conn.execute(
                    "INSERT INTO applications (id, date, company, title, ats_status, url, "
                    "url_norm, folder) VALUES (?, ?, ?, 't', ?, ?, ?, ?)",
                    (rid, day, company, ats, f"https://x/{rid}", f"https://x/{rid}", folder),
                )
        got = {r["company"] for r in get_recent_applied_for_repost(60)}
        assert got == {"Live"}

    def test_add_applied_explicit_reapplication_flag(self, tracker_db) -> None:
        from hunter.db import get_db
        from hunter.tracker import DB_PATH, add_applied

        content = {
            "company_name": "Acme",
            "job_title": "Dev",
            "apply_url": "https://new.example.com/repost-1",
            "output_folder": "Applications/x/Acme",
            "ats_score": "90",
        }
        assert add_applied(content, reapplication=True)
        with get_db(DB_PATH) as conn:
            row = conn.execute(
                "SELECT reapplication FROM applications WHERE company='Acme'"
            ).fetchone()
        assert row["reapplication"] == "+"


# ── 4. execute_reuse ─────────────────────────────────────────────────────────


class TestExecuteReuse:
    def _match(self, folder: Path, sim: float = 0.99) -> RepostMatch:
        return RepostMatch(
            action="reuse",
            similarity=sim,
            donor_url="https://old.example.com/AcmeCorp",
            donor_company="AcmeCorp",
            donor_date="2026-07-01",
            donor_folder=folder,
        )

    def test_reuse_copies_docs_writes_row_and_notifies(
        self, tmp_path, tracker_db, monkeypatch
    ) -> None:
        donor = _make_donor_folder(tmp_path, "AcmeCorp")
        apps_dir = tmp_path / "out"
        monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", apps_dir)
        new_url = "https://new.example.com/acme-repost"

        with (
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.apply_shared.send_telegram_documents") as mock_send,
        ):
            new_folder = execute_reuse(self._match(donor), new_url, _LONG_TEXT)

        assert new_folder is not None and new_folder != donor
        assert (new_folder / "AcmeCorp_CV_EN_ats91.pdf").exists()
        assert (new_folder / "outreach.md").exists()

        content = json.loads((new_folder / "content.json").read_text(encoding="utf-8"))
        assert content["apply_url"] == new_url
        assert content["reused_from"].endswith("AcmeCorp")
        assert content["cost"]["total_usd"] == 0.0

        posting = (new_folder / "job_posting.txt").read_text(encoding="utf-8")
        assert posting.startswith(f"URL: {new_url}")

        from hunter.db import get_db
        from hunter.tracker import DB_PATH

        with get_db(DB_PATH) as conn:
            row = conn.execute(
                "SELECT reapplication, ats_verdict, folder, cost_usd FROM applications "
                "WHERE url_norm LIKE '%acme-repost%'"
            ).fetchone()
        assert row is not None
        assert row["reapplication"] == "+"
        assert row["ats_verdict"] == 91.0
        assert row["cost_usd"] == 0.0
        assert Path(row["folder"]) == new_folder

        mock_notify.assert_called_once()
        assert "Re-post detected" in mock_notify.call_args[0][0]
        mock_send.assert_called_once()

    def test_donor_without_rendered_docs_returns_none(
        self, tmp_path, tracker_db, monkeypatch
    ) -> None:
        donor = _make_donor_folder(tmp_path, "NoDocs", with_pdf=False)
        monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", tmp_path / "out")
        with (
            patch("hunter.apply_shared.notify"),
            patch("hunter.apply_shared.send_telegram_documents"),
        ):
            assert execute_reuse(self._match(donor), "https://x/y", _LONG_TEXT) is None

    def test_any_internal_error_returns_none(self, tmp_path, tracker_db, monkeypatch) -> None:
        donor = tmp_path / "missing"  # no content.json at all
        monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", tmp_path / "out")
        assert execute_reuse(self._match(donor), "https://x/y", _LONG_TEXT) is None


# ── 5. run_repost_gate orchestration + pipeline wiring ───────────────────────


class TestRunRepostGate:
    def test_disabled_is_noop(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", False)
        with patch("hunter.repost_gate.find_repost") as mock_find:
            assert run_repost_gate(_LONG_TEXT, "https://x/y") is None
        mock_find.assert_not_called()

    def test_force_bypasses_gate(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", True)
        with patch("hunter.repost_gate.find_repost") as mock_find:
            assert run_repost_gate(_LONG_TEXT, "https://x/y", is_force_override=True) is None
        mock_find.assert_not_called()

    def test_find_error_degrades_to_continue(self, monkeypatch) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", True)
        with patch("hunter.repost_gate.find_repost", side_effect=RuntimeError("boom")):
            assert run_repost_gate(_LONG_TEXT, "https://x/y") is None

    def test_warn_band_notifies_and_continues(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", True)
        match = RepostMatch("warn", 0.88, "https://old/x", "Hays", "2026-07-01", tmp_path)
        with (
            patch("hunter.repost_gate.find_repost", return_value=match),
            patch("hunter.apply_shared.notify") as mock_notify,
            patch("hunter.repost_gate.execute_reuse") as mock_exec,
        ):
            assert run_repost_gate(_LONG_TEXT, "https://x/y") is None
        mock_notify.assert_called_once()
        assert "Possible re-post" in mock_notify.call_args[0][0]
        mock_exec.assert_not_called()

    def test_reuse_match_returns_folder(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", True)
        match = RepostMatch("reuse", 0.99, "https://old/x", "Acme", "2026-07-01", tmp_path)
        with (
            patch("hunter.repost_gate.find_repost", return_value=match),
            patch("hunter.repost_gate.execute_reuse", return_value=tmp_path / "new") as mock_exec,
        ):
            assert run_repost_gate(_LONG_TEXT, "https://x/y") == tmp_path / "new"
        mock_exec.assert_called_once()

    def test_reuse_failure_falls_back_to_generation(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setattr("hunter.config.REPOST_GATE_ENABLED", True)
        match = RepostMatch("reuse", 0.99, "https://old/x", "Acme", "2026-07-01", tmp_path)
        with (
            patch("hunter.repost_gate.find_repost", return_value=match),
            patch("hunter.repost_gate.execute_reuse", return_value=None),
        ):
            assert run_repost_gate(_LONG_TEXT, "https://x/y") is None


def _patch_api_pre_repost_gate(monkeypatch, job_text: str = _LONG_TEXT) -> None:
    """Neutralize every pipeline stage before Step 1.5g (mirror of the doomed
    gate wiring tests, plus the doomed gate itself returning False)."""
    monkeypatch.setattr("hunter.apply_api._already_processed", lambda *a, **kw: False)
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: job_text)
    monkeypatch.setattr("hunter.validation.is_job_text_too_short", lambda *a, **kw: False)
    monkeypatch.setattr("hunter.expired_check.is_job_expired", lambda text: False)
    monkeypatch.setattr("hunter.apply_api.is_react_only_job_text", lambda text: False)
    monkeypatch.setattr("hunter.apply_api.is_backend_only_job_text", lambda text: False)
    monkeypatch.setattr("hunter.filters.screen_job_text", lambda text: None)
    monkeypatch.setattr("hunter.apply_api.run_doomed_gate", lambda *a, **kw: False)


class TestApiPipelineWiring:
    def test_reused_repost_aborts_generation_and_skips_shadow(self, monkeypatch, tmp_path):
        _patch_api_pre_repost_gate(monkeypatch)
        monkeypatch.setattr(
            "hunter.repost_gate.run_repost_gate", lambda *a, **kw: tmp_path / "reused"
        )
        # Reaching Step 2 would sys.exit(1) on the bogus PROMPTS_DIR — so a
        # clean None return proves the gate stopped the pipeline. None (not
        # the folder) also means apply_agent.main() skips the dual-apply shadow.
        monkeypatch.setattr("hunter.apply_api.PROMPTS_DIR", Path("/nonexistent/prompts"))

        from hunter.apply_api import main_api

        with patch("hunter.apply_api.notify"):
            assert main_api("https://example.com/repost") is None

    def test_no_repost_continues_to_step2(self, monkeypatch) -> None:
        _patch_api_pre_repost_gate(monkeypatch)
        monkeypatch.setattr("hunter.repost_gate.run_repost_gate", lambda *a, **kw: None)
        monkeypatch.setattr("hunter.apply_api.PROMPTS_DIR", Path("/nonexistent/prompts"))

        from hunter.apply_api import main_api

        with patch("hunter.apply_api.notify"):
            with pytest.raises(SystemExit):
                main_api("https://example.com/no-repost")

    def test_force_reaches_gate_with_override(self, monkeypatch) -> None:
        _patch_api_pre_repost_gate(monkeypatch)
        seen = {}

        def _capture(job_text, url, *, company="", permalink="", is_force_override=False):
            seen["force"] = is_force_override
            return None

        monkeypatch.setattr("hunter.repost_gate.run_repost_gate", _capture)
        monkeypatch.setattr("hunter.apply_api.PROMPTS_DIR", Path("/nonexistent/prompts"))

        from hunter.apply_api import main_api

        with patch("hunter.apply_api.notify"), pytest.raises(SystemExit):
            main_api("https://example.com/force", skip_dedup=True)

        assert seen["force"] is True
