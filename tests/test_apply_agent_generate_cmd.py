from pathlib import Path

import apply_agent


def test_build_generate_docs_cmd_uses_content_json_path() -> None:
    content_path = Path("D:/tmp/Applications/2026-04-16/Acme/content.json")
    cmd = apply_agent._build_generate_docs_cmd(content_path, use_full=False, force=False)
    assert cmd[:3] == [apply_agent.sys.executable, str(apply_agent.GENERATE_DOCS_SCRIPT), str(content_path)]


def test_build_generate_docs_cmd_adds_flags() -> None:
    content_path = Path("D:/tmp/Applications/2026-04-16/Acme/content.json")
    cmd = apply_agent._build_generate_docs_cmd(content_path, use_full=True, force=True)
    assert "--full" in cmd
    assert "--force" in cmd
