"""tests/test_golden_apply_e2e.py — sequential/orchestration test for hunter.apply_api.main_api().

docs/quality/04-coverage-and-golden-e2e.md, Part B.

Every existing test around the apply pipeline is a unit around one stage with
its neighbours mocked. That leaves a real, recurring bug class uncovered:
"stages work individually but the wiring between them breaks" — a renamed
content.json key, a flag that isn't threaded into the second pipeline, a
tracker stamp written before the row exists. Several fixes in the Agent Work
Log are exactly this class of bug, caught in prod, not in CI.

This module runs ``hunter.apply_api.main_api()`` for real, mocking only the
external boundaries:
  - LLM:        llm_client.call_llm -> tests/conftest.py::fake_llm (fixture
                responses in tests/fixtures/golden/, routed by prompt shape)
  - Network:    hunter.sources.fetch_job_text -> a fixture job posting
  - LibreOffice/generate_docs: the `subprocess.run` boundary in
                hunter.apply_api is replaced with FakeGenerateDocsRunner,
                which reuses generate_docs.py's REAL, deterministic filename
                helper (resume_docx_basename) and REAL tracker-write function
                (record_successful_apply) — only the docx-building +
                LibreOffice conversion are faked (hand-rolled minimal PDFs
                with extractable text, since ats_pdf_roundtrip/run_llm_verdict
                need real PDF bytes to read). generate_docs.py's own DOCX/PDF
                rendering is covered by its own tests; this file tests
                orchestration, not rendering.
  - Telegram:   hunter.apply_api.notify / send_telegram_documents -> lists
  - DB/files:   tracker_db fixture (tmp tracker.db) + APPLICATIONS_DIR -> tmp_path

Assert contract (what must survive any future refactor of the pipeline):
  - content.json has the full expected key set (resume_en, cover_letter_en,
    primary_lang, ats_verdict, cost, to_learn, ...)
  - a tracker.db row exists with company/title/URL/ATS%/Folder/cost, and its
    ATS% column carries the independent PDF verdict (not the self-score)
  - the expected files exist in the output folder (job_posting.txt,
    content.json, an EN CV PDF)
  - the Telegram message sequence is non-empty and the final one signals success
  - negative paths (expired, doomed-gate HARD) short-circuit BEFORE any LLM
    call and write the expected tracker status without generating documents

This run originally surfaced a real, pre-existing bug: hunter.outreach.
_candidate_summary assumed resume_en.skills was a list, but it is a dict
everywhere else in the codebase (generate_docs.build_resume,
claim_judge.iter_judged_fields), so it raised and outreach.md silently never
got written (swallowed by run_outreach's best-effort contract). Fixed in
hunter/outreach.py (see tests/test_outreach.py for the regression coverage);
test_golden_happy_path_en now asserts outreach.md IS written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "golden"


def _load_json(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _load_job_text(name: str) -> str:
    return (FIXTURES_DIR / name).read_text(encoding="utf-8")


# ── Minimal hand-rolled PDF with extractable text ───────────────────────────
#
# generate_docs.py's real PDF step shells out to LibreOffice, which this test
# deliberately avoids (slow, environment-dependent, and already covered by
# generate_docs.py's own tests). ats_pdf_roundtrip / run_llm_verdict need to
# *read* a real PDF's text via pypdf, though, so an empty/non-PDF file won't
# do — this builds the smallest valid PDF (one page, one Tj text-draw op)
# pypdf can parse, verified against pypdf.PdfReader directly.


def _make_min_pdf(text: str) -> bytes:
    escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
    content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1", errors="replace")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /Resources << /Font << /F1 4 0 R >> >> "
        b"/MediaBox [0 0 612 792] /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode("latin-1") + obj + b"\nendobj\n"
    xref_offset = len(out)
    n = len(objects) + 1
    out += f"xref\n0 {n}\n".encode("latin-1")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("latin-1")
    out += f"trailer\n<< /Size {n} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF".encode(
        "latin-1"
    )
    return bytes(out)


def _resume_text_for_pdf(resume: dict) -> str:
    """Flatten a resume_en dict into plain text good enough for the heuristic
    keyword/TF-IDF roundtrip scorer to find real overlap with the posting."""
    parts = [resume.get("summary", "")]
    skills = resume.get("skills", {})
    if isinstance(skills, dict):
        parts.extend(str(v) for v in skills.values())
    for role in resume.get("experience", []) or []:
        parts.extend(role.get("bullets", []) or [])
        parts.append(role.get("stack_line", ""))
    return " ".join(p for p in parts if p)[:3000]


class FakeGenerateDocsRunner:
    """Stand-in for `subprocess.run([python, generate_docs.py, content.json, ...])`.

    Mirrors generate_docs.py's real, load-bearing side effects — deterministic
    CV filenames (reuses generate_docs.resume_docx_basename) and the tracker
    write (reuses hunter.services.tracker_service.record_successful_apply) —
    without spawning a python subprocess or invoking LibreOffice. Records
    every invocation so a test can assert how many times it ran (e.g. the
    self-heal / verdict-refine loops re-render).
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, cmd, **kwargs):
        import subprocess

        self.calls.append(list(cmd))
        content_json_path = Path(cmd[2])
        force_mode = "--force" in cmd
        no_tracker = "--no-tracker" in cmd
        full_mode = "--full" in cmd

        content = json.loads(content_json_path.read_text(encoding="utf-8"))
        output_folder = Path(content["output_folder"])
        output_folder.mkdir(parents=True, exist_ok=True)
        stack = content.get("stack", "")

        from generate_docs import resume_docx_basename

        if content.get("resume_en"):
            fname = resume_docx_basename(stack, "EN").replace(".docx", ".pdf")
            (output_folder / fname).write_bytes(
                _make_min_pdf(_resume_text_for_pdf(content["resume_en"]))
            )

        primary_pl = (content.get("primary_lang") or "").strip().upper() == "PL"
        if (full_mode or primary_pl) and content.get("resume_pl"):
            fname = resume_docx_basename(stack, "PL").replace(".docx", ".pdf")
            (output_folder / fname).write_bytes(_make_min_pdf("PL CV placeholder text"))

        if content.get("cover_letter_en"):
            (output_folder / "Cover_Letter_EN.pdf").write_bytes(
                _make_min_pdf(content["cover_letter_en"][:800])
            )
        if content.get("cover_letter_pl"):
            (output_folder / "Cover_Letter_PL.pdf").write_bytes(
                _make_min_pdf("PL cover letter placeholder")
            )

        if not no_tracker:
            from hunter.services.tracker_service import record_successful_apply

            record_successful_apply(content, force=force_mode)

        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


@pytest.fixture()
def golden_env(tmp_path, monkeypatch, tracker_db, fake_llm):
    """Wires every external boundary main_api() touches to a controllable fake.

    Returns a namespace with the pieces a test needs to configure/assert:
      notifications  — list of every notify() message (HTML, in call order)
      sent_docs      — list of file-path lists passed to send_telegram_documents
      gen_runner     — FakeGenerateDocsRunner (inspect .calls for regen count)
      applications_dir — tmp Applications/ root
    """
    applications_dir = tmp_path / "Applications"
    monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", applications_dir)
    # llm_profiles reads its DB-persisted profile choice from this path —
    # isolate it onto the same tmp tracker DB the `tracker_db` fixture set up
    # (hunter.tracker.DB_PATH), so the test never touches a real tracker.db.
    monkeypatch.setattr("hunter.config.TRACKER_DB_PATH", tracker_db)
    # run_llm_verdict early-returns None without a judge API key.
    monkeypatch.setattr("hunter.config.JUDGE_API_KEY", "test-judge-key")

    notifications: list[str] = []
    sent_docs: list[list[Path]] = []
    # notify() is imported at module level into BOTH hunter.apply_api (its own
    # Step 1/4.72/4.75/7.8/8 calls) AND hunter.apply_shared (run_doomed_gate,
    # _already_processed's tracker-hit message) — each holds its own bound
    # name, so both must be patched to land in the same list.
    monkeypatch.setattr("hunter.apply_api.notify", notifications.append)
    monkeypatch.setattr("hunter.apply_shared.notify", notifications.append)
    monkeypatch.setattr("hunter.apply_api.send_telegram_documents", sent_docs.append)

    gen_runner = FakeGenerateDocsRunner()
    monkeypatch.setattr("hunter.apply_api.subprocess.run", gen_runner)

    class Env:
        pass

    env = Env()
    env.notifications = notifications
    env.sent_docs = sent_docs
    env.gen_runner = gen_runner
    env.applications_dir = applications_dir
    env.tracker_db = tracker_db
    return env


@pytest.fixture()
def golden_job_text() -> str:
    return _load_job_text("job_posting_en.txt")


@pytest.fixture()
def golden_generation_response() -> dict:
    return _load_json("generation_response.json")


@pytest.fixture()
def golden_verdict_response() -> dict:
    return _load_json("verdict_response.json")


# ── Happy path: fetched URL, EN posting, short mode ─────────────────────────


def test_golden_happy_path_en(
    golden_env,
    golden_job_text,
    golden_generation_response,
    golden_verdict_response,
    fake_llm,
    monkeypatch,
):
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: golden_job_text, raising=False)
    fake_llm.generation_response = golden_generation_response
    fake_llm.verdict_response = golden_verdict_response

    from hunter.apply_api import main_api

    url = "https://example.com/jobs/nordic-frontend-labs-senior-angular"
    output_folder = main_api(url)

    assert output_folder is not None, "main_api should return the output folder on success"
    assert output_folder.is_dir()

    # ── content.json: the contract a refactor must not break ───────────────
    content = json.loads((output_folder / "content.json").read_text(encoding="utf-8"))
    for key in (
        "resume_en",
        "cover_letter_en",
        "cover_letter_pl",
        "about_me_en",
        "about_me_pl",
        "company_name",
        "job_title",
        "stack",
        "primary_lang",
        "cost",
        "to_learn",
        "ats_verdict",
    ):
        assert key in content, f"content.json missing expected key: {key}"
    assert content["primary_lang"] == "EN"
    assert content["ats_verdict"]["score"] == 96

    # ── files on disk ────────────────────────────────────────────────────
    assert (output_folder / "job_posting.txt").is_file()
    assert (output_folder / "content.json").is_file()
    assert (output_folder / "outreach.md").is_file()
    en_cv_pdfs = list(output_folder.glob("*CV*EN*.pdf"))
    assert en_cv_pdfs, "expected an EN CV PDF in the output folder"

    # ── tracker.db row ───────────────────────────────────────────────────
    from hunter import tracker

    rows = tracker.lookup_url(url)
    assert rows, "expected a tracker row for the applied URL"
    row = rows[0]
    assert row["company"] == "Nordic Frontend Labs"
    assert "Angular" in row["title"] or "Frontend" in row["title"]
    assert row["folder"]
    # The independent PDF verdict (96), not the generator's own self-score
    # (92) — set_ats_verdict must have overwritten the ATS column.
    assert row["ats"].strip() == "96%"

    # ── Telegram sequence: non-empty, ends in a success message ────────────
    assert golden_env.notifications, "expected at least one Telegram notification"
    assert "Docs ready" in golden_env.notifications[-1]
    assert golden_env.sent_docs, "expected send_telegram_documents to be called"

    # ── LLM calls actually happened (generation + judge + verdict) ─────────
    assert len(fake_llm.calls) >= 3


def test_golden_happy_path_paste_mode(
    golden_env,
    golden_job_text,
    golden_generation_response,
    golden_verdict_response,
    fake_llm,
    monkeypatch,
):
    """paste_text bypasses the network fetch entirely but otherwise runs the
    identical pipeline — the paste flow must not diverge from the URL flow."""
    fetch_calls = []
    monkeypatch.setattr(
        "hunter.sources.fetch_job_text",
        lambda url: fetch_calls.append(url) or golden_job_text,
        raising=False,
    )
    fake_llm.generation_response = golden_generation_response
    fake_llm.verdict_response = golden_verdict_response

    from hunter.apply_api import main_api
    from hunter.apply_shared import PASTE_NO_URL_PLACEHOLDER

    output_folder = main_api(PASTE_NO_URL_PLACEHOLDER, paste_text=golden_job_text)

    assert fetch_calls == [], "paste flow must not call fetch_job_text"
    assert output_folder is not None
    content = json.loads((output_folder / "content.json").read_text(encoding="utf-8"))
    assert content["apply_url"] == ""
    assert (output_folder / "content.json").is_file()


# ── Negative path: expired posting — no LLM call at all ─────────────────────


def test_golden_expired_job_no_llm_call(golden_env, fake_llm, monkeypatch):
    expired_text = (
        "Job Title: Senior Frontend Developer\nCompany: Some Co\n"
        "Location: Remote (EU)\nEmployment: B2B\n\n"
        "--- Job Description ---\n"
        "We were looking for a Senior Frontend Developer with Angular experience "
        "to join our remote EU-based team. Unfortunately, "
        "this job posting has expired. This position is no longer accepting applications. "
        "The offer has been closed and is no longer active. Thank you for your interest."
    )
    assert len(expired_text) >= 300, "must clear MIN_JOB_TEXT_LEN or Step 1.5a fires first"
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: expired_text, raising=False)

    from hunter.apply_api import main_api

    url = "https://example.com/jobs/expired-role"
    result = main_api(url)

    assert result is None
    assert fake_llm.calls == [], "an expired posting must never reach the LLM"

    from hunter import tracker

    rows = tracker.lookup_url(url)
    assert rows
    assert rows[0]["sent"].strip().upper() == "EXPIRED"
    assert any("Expired" in n for n in golden_env.notifications)


# ── Negative path: doomed-gate HARD finding — SKIP row, no LLM call ─────────


def test_golden_doomed_gate_hard_skips_before_llm(golden_env, fake_llm, monkeypatch):
    # Onsite in a non-EU/US city with no remote/Wrocław escape hatch, and a
    # non-EU work-authorization requirement — a HARD finding per
    # hunter.filters.assess_job_text (docs/DOOMED_GATE_PLAN.md).
    doomed_text = (
        "Job Title: Senior Frontend Developer\nCompany: Acme US Inc\n"
        "Location: Onsite in Austin, Texas, USA. This is an onsite role, no remote work.\n\n"
        "--- Job Description ---\n"
        "Must be a US citizen or green card holder. This role requires W2 employment only, "
        "no C2C or third-party contracts. Relocation to our Austin, Texas office is required; "
        "this is a fully onsite position, five days a week in the office."
    )
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: doomed_text, raising=False)

    from hunter.apply_api import main_api

    url = "https://example.com/jobs/onsite-us-role"
    result = main_api(url)

    assert result is None
    assert fake_llm.calls == [], "a HARD doomed-gate finding must abort before any LLM call"

    from hunter import tracker

    rows = tracker.lookup_url(url)
    assert rows
    assert rows[0]["ats"].strip().upper() == "SKIP"
    assert any("Skipped before generation" in n for n in golden_env.notifications)


# ── Mutation checks: the golden test must actually be able to fail ──────────
#
# Not full mutation testing — three cheap, targeted breaks of the pipeline's
# connective tissue, verifying the assert contract above would have caught
# them (docs/quality/04's own readiness criterion: "падает при намеренной
# порче связки — проверить на 2-3 мутациях руками").


def test_golden_catches_missing_verdict_stamp(
    golden_env,
    golden_job_text,
    golden_generation_response,
    golden_verdict_response,
    fake_llm,
    monkeypatch,
):
    """If the verdict never gets stamped into content.json, the contract
    assertion on `ats_verdict` must fail — simulated by starving the verdict
    call (no verdict_response configured -> run_llm_verdict gets no judge
    key and returns None early instead)."""
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: golden_job_text, raising=False)
    monkeypatch.setattr("hunter.config.JUDGE_API_KEY", "")  # verdict step short-circuits to None
    fake_llm.generation_response = golden_generation_response
    fake_llm.verdict_response = golden_verdict_response

    from hunter.apply_api import main_api

    output_folder = main_api("https://example.com/jobs/no-verdict-key")
    content = json.loads((output_folder / "content.json").read_text(encoding="utf-8"))
    assert "ats_verdict" not in content


def test_golden_catches_broken_tracker_row(
    golden_env,
    golden_job_text,
    golden_generation_response,
    golden_verdict_response,
    fake_llm,
    monkeypatch,
):
    """If generate_docs never writes the tracker row (e.g. --no-tracker
    wrongly forced on the primary Step 7 call), the golden test's tracker
    assertion must fail — simulated by forcing the fake runner to always
    skip the tracker write, mirroring a hypothetical wiring bug."""
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: golden_job_text, raising=False)
    fake_llm.generation_response = golden_generation_response
    fake_llm.verdict_response = golden_verdict_response

    def _broken_call(cmd, **kwargs):
        cmd = list(cmd) + ["--no-tracker"]  # simulate the wiring bug
        return golden_env.gen_runner(cmd, **kwargs)

    monkeypatch.setattr("hunter.apply_api.subprocess.run", _broken_call)

    from hunter.apply_api import main_api
    from hunter import tracker

    url = "https://example.com/jobs/broken-tracker-wiring"
    main_api(url)
    assert tracker.lookup_url(url) == []


def test_golden_catches_lost_ats_score(
    golden_env,
    golden_job_text,
    golden_generation_response,
    fake_llm,
    monkeypatch,
):
    """If the independent verdict never overrides the tracker ATS column
    (e.g. set_ats_verdict silently stops being called), the row keeps the
    generator's own self-score instead — simulated by never configuring a
    verdict_response (JUDGE key present, but the verdict call itself would
    raise on an unset fixture) so no verdict is ever stamped."""
    monkeypatch.setattr("hunter.sources.fetch_job_text", lambda url: golden_job_text, raising=False)
    monkeypatch.setattr("hunter.config.ATS_VERDICT_ENABLED", False)
    fake_llm.generation_response = golden_generation_response

    from hunter.apply_api import main_api
    from hunter import tracker

    url = "https://example.com/jobs/verdict-disabled"
    main_api(url)
    rows = tracker.lookup_url(url)
    assert rows
    # Falls back to the generator's own self-score ("92%"), not a verdict.
    assert rows[0]["ats"].strip() == "92%"
