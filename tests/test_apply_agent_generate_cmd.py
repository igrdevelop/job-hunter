from pathlib import Path

from hunter.services.apply_service import build_generate_docs_cmd


def test_build_generate_docs_cmd_uses_content_json_path() -> None:
    content_path = Path("D:/tmp/Applications/2026-04-16/Acme/content.json")
    cmd = build_generate_docs_cmd(
        generate_docs_script=Path("D:/LearningProject/Claude/generate_docs.py"),
        content_json_path=content_path,
        use_full=False,
        force=False,
        python_executable="python",
    )
    assert cmd[:3] == ["python", str(Path("D:/LearningProject/Claude/generate_docs.py")), str(content_path)]


def test_build_generate_docs_cmd_adds_flags() -> None:
    content_path = Path("D:/tmp/Applications/2026-04-16/Acme/content.json")
    cmd = build_generate_docs_cmd(
        generate_docs_script=Path("D:/LearningProject/Claude/generate_docs.py"),
        content_json_path=content_path,
        use_full=True,
        force=True,
        python_executable="python",
    )
    assert "--full" in cmd
    assert "--force" in cmd
