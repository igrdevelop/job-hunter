"""tests/test_golden_apply_cli_e2e.py — the CLI pipeline's missing safety net.

docs/STACK_PRESCREEN_PLAN.md M7.

`tests/test_golden_apply_e2e.py` runs `main_api` for real and has caught the
"stages work individually but the wiring breaks" bug class ever since. The CLI
pipeline had no equivalent — and it is the branch that keeps drifting, because
every stage is mirrored into it by hand. Four production incidents in five weeks
came from exactly that gap:

  2026-08-22  primary_lang was stamped only as a side effect of a repair, so a
              clean CLI run left it absent and silently disabled BOTH the PL-CV
              routing and the verdict-refine PL mirror
  2026-08-22  the skill was told to return "resume_pl": null unless --full,
              unconditionally, so Polish employers received an English CV
  2026-08-24  the React-only gate ran after the docs and the tracker row already
              existed, so the package was delivered anyway
  2026-08-24  the company+title dedup gate had the same shape and the same bug

Each scenario below reproduces one of them.

Only the external boundaries are faked: the `claude -p` subprocess (replaced by
a stand-in that behaves like the real skill — it creates the folder, writes
content.json and runs generate_docs WITHOUT --no-tracker, which is what
`.claude/commands/apply.md` tells it to do), the network, the LLM, and
LibreOffice. Everything between them is the real `main_cli`.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from hunter import tracker
from tests.test_golden_apply_e2e import FakeGenerateDocsRunner

EN_POSTING = (
    "Job Title: Senior Angular Developer\nCompany: Nordic Frontend Labs\n"
    "Location: Fully remote (Poland)\n\n"
    "--- Job Description ---\n"
    "We are looking for a senior Angular engineer to own our design system. You "
    "will work with Angular, TypeScript and RxJS across several products, mentor "
    "other engineers and shape the frontend architecture. Fully remote within "
    "Poland, contract of employment or B2B, with a yearly training budget."
)

PL_POSTING = (
    "Stanowisko: Starszy Programista Angular\nFirma: Nordic Frontend Labs\n"
    "Lokalizacja: Praca zdalna (Polska)\n\n"
    "--- Opis stanowiska ---\n"
    "Poszukujemy doświadczonego programisty Angular, który poprowadzi rozwój "
    "naszego systemu projektowego. Będziesz pracować z Angular, TypeScript i "
    "RxJS, wspierać zespół oraz kształtować architekturę frontendu. Praca w "
    "pełni zdalna na terenie Polski, umowa o pracę lub B2B."
)


class FakeClaudeSkill:
    """Stand-in for the `claude -p "/apply ..."` subprocess.

    Does what the real skill does and nothing more: creates the dated output
    folder, writes content.json into it, and runs generate_docs.py on that file
    WITHOUT --no-tracker — so, exactly like production, the tracker row exists
    by the time main_cli's own post-processing starts. Re-render calls that
    main_cli makes later are routed to the same generate_docs stand-in.
    """

    def __init__(self, content: dict, applications_dir: Path, gen_runner) -> None:
        self.content = content
        self.applications_dir = applications_dir
        self.gen_runner = gen_runner
        self.claude_calls = 0

    def __call__(self, cmd, **kwargs):
        import subprocess

        if any("generate_docs" in str(part) for part in cmd):
            return self.gen_runner(cmd, **kwargs)

        self.claude_calls += 1
        folder = self.applications_dir / date.today().strftime("%Y-%m-%d") / "NordicFrontendLabs"
        folder.mkdir(parents=True, exist_ok=True)
        content = dict(self.content)
        content["output_folder"] = str(folder)
        content_path = folder / "content.json"
        content_path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")
        self.gen_runner(["python", "generate_docs.py", str(content_path)], **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout="Package ready.", stderr="")


GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden"


def _golden(name: str) -> dict:
    return json.loads((GOLDEN_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture()
def golden_generation_response() -> dict:
    # Same fixture files the API golden suite uses -- one shape for both
    # pipelines is the point: they are supposed to produce the same package.
    return _golden("generation_response.json")


@pytest.fixture()
def golden_verdict_response() -> dict:
    return _golden("verdict_response.json")


@pytest.fixture()
def cli_env(
    tmp_path,
    monkeypatch,
    tracker_db,
    fake_llm,
    golden_generation_response,
    golden_verdict_response,
):
    """Wires every boundary main_cli touches. Returns a configurable namespace."""
    applications = tmp_path / "Applications"
    applications.mkdir()
    monkeypatch.setattr("hunter.apply_shared.APPLICATIONS_DIR", applications)
    monkeypatch.setattr("hunter.apply_cli.APPLICATIONS_DIR", applications)
    monkeypatch.setattr("hunter.config.JUDGE_API_KEY", "test-judge-key")

    notifications: list[str] = []
    monkeypatch.setattr("hunter.apply_cli.notify", notifications.append)
    monkeypatch.setattr("hunter.apply_shared.notify", notifications.append)
    monkeypatch.setattr("hunter.apply_cli.send_telegram_documents", lambda _paths: None)

    fake_llm.generation_response = golden_generation_response
    fake_llm.verdict_response = golden_verdict_response

    # main_cli polls for the new folder for 30 wall-clock seconds. Keep the real
    # detection logic, collapse the deadline.
    from hunter import apply_cli as _mod

    _real_find = _mod._find_new_folder
    monkeypatch.setattr(
        "hunter.apply_cli._find_new_folder",
        lambda before, timeout=0: _real_find(before, timeout=0),
    )

    class _Env:
        def __init__(self):
            self.applications = applications
            self.notifications = notifications
            self.gen_runner = FakeGenerateDocsRunner()
            self.skill = None

        def run(self, url: str, posting: str, content: dict, **kwargs):
            monkeypatch.setattr(
                "hunter.sources.fetch_job_text", lambda _u, **_kw: posting, raising=False
            )
            self.skill = FakeClaudeSkill(content, applications, self.gen_runner)
            monkeypatch.setattr("hunter.apply_cli.subprocess.run", self.skill)
            from hunter.apply_cli import main_cli

            return main_cli(url, **kwargs)

    return _Env()


def _content(generation: dict, **overrides) -> dict:
    base = dict(generation)
    base.setdefault("apply_url", "")
    base.update(overrides)
    return base


def _row(url: str) -> dict:
    rows = tracker.lookup_url(url)
    return rows[0] if rows else {}


class TestPolishPostingShipsAPolishCv:
    """2026-08-22: 15 of 250 PL applications shipped an English CV."""

    URL = "https://example.com/jobs/pl-angular"

    def test_pl_cv_is_rendered_even_when_the_skill_returns_null(
        self, cli_env, golden_generation_response
    ):
        # The prompt used to say "resume_pl": null unless --full, unconditionally.
        content = _content(golden_generation_response, apply_url=self.URL, resume_pl=None)

        folder = cli_env.run(self.URL, PL_POSTING, content)

        assert folder is not None
        pl_cvs = list(folder.glob("*_PL.pdf"))
        assert pl_cvs, (
            "a Polish posting must ship a Polish CV; the net under the prompt is "
            "apply_shared.ensure_pl_resume"
        )

    def test_primary_lang_is_persisted(self, cli_env, golden_generation_response):
        # It used to be stamped only as a side effect of a repair, so a clean run
        # left it absent -- and it gates both the PL routing and the refine mirror.
        content = _content(golden_generation_response, apply_url=self.URL)

        folder = cli_env.run(self.URL, PL_POSTING, content)

        written = json.loads((folder / "content.json").read_text(encoding="utf-8"))
        assert written.get("primary_lang") == "PL"


class TestEnglishPostingIsUnaffected:
    URL = "https://example.com/jobs/en-angular"

    def test_a_clean_run_stamps_primary_lang_and_delivers(
        self, cli_env, golden_generation_response
    ):
        content = _content(golden_generation_response, apply_url=self.URL)

        folder = cli_env.run(self.URL, EN_POSTING, content)

        assert folder is not None
        written = json.loads((folder / "content.json").read_text(encoding="utf-8"))
        assert written.get("primary_lang") == "EN"
        assert list(folder.glob("*_EN.pdf")), "the English CV is the deliverable"
        assert _row(self.URL).get("ats", "").strip().endswith("%")


class TestPostGenerationAbortsUndoTheRow:
    """2026-08-24: the gates ran after the row existed, so it shipped anyway."""

    def test_react_only_stack(self, cli_env, golden_generation_response):
        url = "https://example.com/jobs/react-only"
        content = _content(golden_generation_response, apply_url=url, stack="React")

        result = cli_env.run(url, EN_POSTING, content)

        assert result is None, "an aborted run must not return a folder to deliver"
        assert _row(url).get("ats", "").strip().upper() == "SKIP"
        assert not tracker.has_successful_entry(url), "the parent must not deliver this"
        folder = cli_env.applications / date.today().strftime("%Y-%m-%d") / "NordicFrontendLabs"
        assert not list(folder.glob("*.pdf")), "the rendered documents must be gone"
        assert (folder / "job_posting.txt").exists(), "diagnostics stay on purpose"

    def test_company_and_title_already_applied(self, cli_env, golden_generation_response):
        # Same shape, same bug: the manual entry points never run the hunt loop's
        # dedup_key check, so this gate is the only one that can catch a re-post
        # under a new URL -- and it, too, ran after the row was written.
        tracker.add_applied(
            {
                "company_name": golden_generation_response["company_name"],
                "job_title": golden_generation_response["job_title"],
                "apply_url": "https://example.com/jobs/the-first-one",
                "stack": "Angular",
                "ats_score": "94",
                "output_folder": "/tmp/earlier",
            }
        )
        url = "https://example.com/jobs/same-role-new-url"
        content = _content(golden_generation_response, apply_url=url)

        result = cli_env.run(url, EN_POSTING, content)

        assert result is None
        assert _row(url).get("ats", "").strip().upper() == "SKIP"
        assert tracker.has_successful_entry("https://example.com/jobs/the-first-one"), (
            "the ORIGINAL application must survive untouched"
        )

    def test_force_bypasses_the_stack_gate(self, cli_env, golden_generation_response):
        url = "https://example.com/jobs/react-forced"
        content = _content(golden_generation_response, apply_url=url, stack="React")

        folder = cli_env.run(url, EN_POSTING, content, skip_dedup=True)

        assert folder is not None, "/force means generate this one anyway"
        assert _row(url).get("ats", "").strip().endswith("%")


class TestTheSkillIsCalledOnce:
    def test_no_accidental_second_generation(self, cli_env, golden_generation_response):
        # Re-renders (PL mirror, language repair, verdict refine) must go through
        # generate_docs, never through another `claude -p` round.
        url = "https://example.com/jobs/one-call"
        content = _content(golden_generation_response, apply_url=url)

        cli_env.run(url, PL_POSTING, content)

        assert cli_env.skill.claude_calls == 1
