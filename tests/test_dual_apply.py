"""Tests for dual-apply (A/B comparison) mode: profile override, dual config,
and the shadow filename/ATS-suffix helpers."""

import json

import hunter.llm_profiles as lp
from hunter import dual_apply


# ── Profile override (get_active) ───────────────────────────────────────────────

def test_set_override_wins_over_db_and_env(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _k: "sonnet")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-o")
    shadow = lp.PROFILES["deepseek-v3"]
    try:
        lp.set_override(shadow)
        assert lp.get_active().name == "deepseek-v3"
    finally:
        lp.set_override(None)
    # Cleared → back to DB choice
    assert lp.get_active().name == "sonnet"


def test_override_none_is_noop(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _k: None)
    monkeypatch.delenv("LLM_DEFAULT_PROFILE", raising=False)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-a")
    lp.set_override(None)
    # Should resolve normally without raising
    assert lp.get_active() is not None


# ── Dual config (enabled / shadow profile) ──────────────────────────────────────

def test_dual_enabled_reads_db(monkeypatch):
    store = {}
    monkeypatch.setattr(lp, "_db_get", lambda k: store.get(k))
    monkeypatch.setattr(lp, "_db_set", lambda k, v: store.update({k: v}))
    assert lp.dual_enabled() is False
    lp.set_dual(True)
    assert store[lp._DUAL_KEY] == "1"
    assert lp.dual_enabled() is True
    lp.set_dual(False)
    assert lp.dual_enabled() is False


def test_shadow_profile_defaults_to_deepseek_v3(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _k: None)
    monkeypatch.delenv("DUAL_SHADOW_PROFILE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-o")
    p = lp.shadow_profile()
    assert p is not None and p.name == "deepseek-v3"


def test_shadow_profile_none_when_key_missing(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _k: None)
    monkeypatch.delenv("DUAL_SHADOW_PROFILE", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert lp.shadow_profile() is None


def test_shadow_profile_respects_env_override(monkeypatch):
    monkeypatch.setattr(lp, "_db_get", lambda _k: None)
    monkeypatch.setenv("DUAL_SHADOW_PROFILE", "deepseek-r1")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-o")
    p = lp.shadow_profile()
    assert p is not None and p.name == "deepseek-r1"


# ── ATS suffix + filename rename helpers ────────────────────────────────────────

def test_ats_suffix_from_content():
    assert dual_apply._ats_suffix({"ats_check": {"score": 87.5}}) == "_ats88"
    assert dual_apply._ats_suffix({"ats_check": {"score": 92.0}}) == "_ats92"


def test_ats_suffix_missing_is_empty():
    assert dual_apply._ats_suffix({}) == ""
    assert dual_apply._ats_suffix({"ats_check": {}}) == ""


def test_suffix_docs_renames_only_documents(tmp_path):
    (tmp_path / "Ihar_CV_Angular_EN.pdf").write_text("x", encoding="utf-8")
    (tmp_path / "Cover_Letter_EN.docx").write_text("x", encoding="utf-8")
    (tmp_path / "content.json").write_text("{}", encoding="utf-8")
    (tmp_path / "job_posting.txt").write_text("x", encoding="utf-8")

    n = dual_apply._suffix_docs(tmp_path, "_ats88")
    assert n == 2

    names = {p.name for p in tmp_path.iterdir()}
    assert "Ihar_CV_Angular_EN_ats88.pdf" in names
    assert "Cover_Letter_EN_ats88.docx" in names
    # Non-document files untouched
    assert "content.json" in names
    assert "job_posting.txt" in names


def test_suffix_docs_noop_when_suffix_empty(tmp_path):
    (tmp_path / "a.pdf").write_text("x", encoding="utf-8")
    assert dual_apply._suffix_docs(tmp_path, "") == 0
    assert (tmp_path / "a.pdf").exists()


def test_suffix_docs_idempotent(tmp_path):
    f = tmp_path / "CV_EN.pdf"
    f.write_text("x", encoding="utf-8")
    dual_apply._suffix_docs(tmp_path, "_ats80")
    # Second pass must not double-suffix the already-renamed file.
    again = dual_apply._suffix_docs(tmp_path, "_ats80")
    assert again == 0
    assert (tmp_path / "CV_EN_ats80.pdf").exists()


# ── _read_job_text strips the URL header ────────────────────────────────────────

def test_read_job_text_strips_url_header(tmp_path):
    (tmp_path / "job_posting.txt").write_text(
        "URL: https://example.com/job\n\nWe need a Senior Angular dev.",
        encoding="utf-8",
    )
    assert dual_apply._read_job_text(tmp_path) == "We need a Senior Angular dev."


def test_read_job_text_no_header(tmp_path):
    (tmp_path / "job_posting.txt").write_text("Plain body text.", encoding="utf-8")
    assert dual_apply._read_job_text(tmp_path) == "Plain body text."


# ── run_shadow guard clauses (no LLM calls) ─────────────────────────────────────

def test_run_shadow_skips_when_disabled(monkeypatch, tmp_path):
    monkeypatch.setattr(lp, "dual_enabled", lambda: False)
    assert dual_apply.run_shadow(tmp_path) is None


def test_run_shadow_skips_when_shadow_unavailable(monkeypatch, tmp_path):
    monkeypatch.setattr(lp, "dual_enabled", lambda: True)
    monkeypatch.setattr(lp, "shadow_profile", lambda: None)
    assert dual_apply.run_shadow(tmp_path) is None


def test_run_shadow_skips_when_shadow_equals_active(monkeypatch, tmp_path):
    prof = lp.PROFILES["deepseek-v3"]
    monkeypatch.setattr(lp, "dual_enabled", lambda: True)
    monkeypatch.setattr(lp, "shadow_profile", lambda: prof)
    monkeypatch.setattr(lp, "get_active", lambda: prof)
    assert dual_apply.run_shadow(tmp_path) is None


def test_run_shadow_skips_when_job_text_short(monkeypatch, tmp_path):
    shadow = lp.PROFILES["deepseek-v3"]
    active = lp.PROFILES["sonnet"]
    monkeypatch.setattr(lp, "dual_enabled", lambda: True)
    monkeypatch.setattr(lp, "shadow_profile", lambda: shadow)
    monkeypatch.setattr(lp, "get_active", lambda: active)
    (tmp_path / "job_posting.txt").write_text("URL: x\n\nshort", encoding="utf-8")
    # Should bail on the <100 char guard without calling the LLM.
    assert dual_apply.run_shadow(tmp_path) is None


# ── Full orchestration (LLM + generate_docs mocked) ─────────────────────────────

def test_generate_shadow_writes_subfolder_no_tracker(monkeypatch, tmp_path):
    """End-to-end orchestration: content.json in {shadow}/, generate_docs called
    with --no-tracker, docs suffixed with the ATS score."""
    shadow = lp.PROFILES["deepseek-v3"]
    active = lp.PROFILES["sonnet"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-o")
    monkeypatch.setattr(lp, "dual_enabled", lambda: True)
    monkeypatch.setattr(lp, "shadow_profile", lambda: shadow)
    monkeypatch.setattr(lp, "get_active", lambda: active)

    job_text = "We are hiring a Senior Angular developer. " * 10  # > 100 chars
    (tmp_path / "job_posting.txt").write_text(f"URL: https://x/y\n\n{job_text}", encoding="utf-8")
    (tmp_path / "content.json").write_text(
        json.dumps({"apply_url": "https://x/y"}), encoding="utf-8"
    )

    generated = {"company_name": "Acme", "stack": "Angular", "resume_en": {"experience": []}}

    monkeypatch.setattr(
        "llm_client.call_llm",
        lambda **kw: dict(generated),
    )
    # Skip the real ATS loop / scrubs / lang gate — exercise orchestration only.
    import hunter.apply_shared as ash
    monkeypatch.setattr(ash, "validate_content", lambda c: [])
    monkeypatch.setattr(
        ash, "_ats_check_loop",
        lambda content, jt: {**content, "ats_check": {"score": 88.0}},
    )
    monkeypatch.setattr(ash, "_strip_compliance_claims", lambda c: (c, []))
    monkeypatch.setattr(ash, "_strip_prestige_claims", lambda c, jt: (c, []))
    monkeypatch.setattr(ash, "_dedup_skill_glosses", lambda c: (c, []))
    monkeypatch.setattr(ash, "enforce_language_separation", lambda c: (c, False, []))
    monkeypatch.setattr("hunter.resume_sanitizer.sanitize_content", lambda c: c)

    captured_cmd = {}

    def fake_run(cmd, **kw):
        captured_cmd["cmd"] = cmd
        # Simulate generate_docs producing a CV + cover letter in the sub folder.
        sub = tmp_path / shadow.name
        (sub / "Ihar_CV_Angular_EN.pdf").write_text("pdf", encoding="utf-8")
        (sub / "Cover_Letter_EN.pdf").write_text("pdf", encoding="utf-8")

        class R:
            returncode = 0
            stdout = ""
            stderr = ""
        return R()

    monkeypatch.setattr(dual_apply.subprocess, "run", fake_run)

    result = dual_apply.run_shadow(tmp_path)

    sub = tmp_path / shadow.name
    assert result == sub
    assert (sub / "content.json").exists()
    written = json.loads((sub / "content.json").read_text(encoding="utf-8"))
    assert written["output_folder"].endswith(shadow.name)
    assert written["apply_url"] == "https://x/y"
    # generate_docs invoked with --no-tracker
    assert "--no-tracker" in captured_cmd["cmd"]
    # Docs suffixed with the shadow's ATS score
    names = {p.name for p in sub.iterdir()}
    assert "Ihar_CV_Angular_EN_ats88.pdf" in names
    assert "Cover_Letter_EN_ats88.pdf" in names


# ── Detached CLI shim (_main) ───────────────────────────────────────────────────

def test_main_usage_without_folder_returns_2():
    assert dual_apply._main(["prog"]) == 2


def test_main_calls_run_shadow_with_full_flag(monkeypatch):
    captured = {}

    def fake_run_shadow(folder, *, full_mode):
        captured["folder"] = folder
        captured["full"] = full_mode

    monkeypatch.setattr(dual_apply, "run_shadow", fake_run_shadow)
    rc = dual_apply._main(["prog", "/app/Applications/2026-06-28/Acme", "--full"])
    assert rc == 0
    assert captured["folder"] == "/app/Applications/2026-06-28/Acme"
    assert captured["full"] is True


def test_main_swallows_shadow_errors(monkeypatch):
    def boom(folder, *, full_mode):
        raise RuntimeError("shadow blew up")

    monkeypatch.setattr(dual_apply, "run_shadow", boom)
    # Must not raise — best-effort detached process.
    assert dual_apply._main(["prog", "/some/folder"]) == 0


# ── launch_detached (fire-and-forget) ───────────────────────────────────────────

def test_launch_detached_noop_when_dual_disabled(monkeypatch):
    monkeypatch.setattr(lp, "dual_enabled", lambda: False)
    called = {"popen": False}
    monkeypatch.setattr(dual_apply.subprocess, "Popen",
                        lambda *a, **k: called.__setitem__("popen", True))
    assert dual_apply.launch_detached("/app/Applications/2026-06-28/Acme") is False
    assert called["popen"] is False


def test_launch_detached_spawns_when_enabled(monkeypatch):
    monkeypatch.setattr(lp, "dual_enabled", lambda: True)
    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return object()

    monkeypatch.setattr(dual_apply.subprocess, "Popen", fake_popen)
    assert dual_apply.launch_detached("/app/Applications/2026-06-28/Acme", full_mode=True) is True
    assert "hunter.dual_apply" in captured["cmd"]
    assert "/app/Applications/2026-06-28/Acme" in captured["cmd"]
    assert "--full" in captured["cmd"]
    # Detached: stdio redirected away so it can't tie to the parent.
    assert captured["kwargs"].get("stdout") is not None


def test_maybe_run_shadow_noop_when_folder_none(monkeypatch):
    import apply_agent
    called = {"launch": False}
    monkeypatch.setattr(dual_apply, "launch_detached",
                        lambda *a, **k: called.__setitem__("launch", True))
    apply_agent._maybe_run_shadow(None, full=False)
    assert called["launch"] is False


def test_maybe_run_shadow_delegates_to_launch_detached(monkeypatch):
    import apply_agent
    captured = {}
    monkeypatch.setattr(dual_apply, "launch_detached",
                        lambda folder, *, full_mode: captured.update(folder=folder, full=full_mode))
    apply_agent._maybe_run_shadow("/app/Applications/2026-06-28/Acme", full=True)
    assert captured["folder"] == "/app/Applications/2026-06-28/Acme"
    assert captured["full"] is True
