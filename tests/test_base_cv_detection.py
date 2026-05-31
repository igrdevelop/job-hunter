"""Tests for base CV stack detection and loading logic in apply_api.py."""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from hunter.apply_api import _detect_stack_hint, _load_base_cv


# ---------------------------------------------------------------------------
# _detect_stack_hint
# ---------------------------------------------------------------------------

class TestDetectStackHint:

    # AI track
    def test_ai_llm_keyword(self):
        assert _detect_stack_hint("We need an LLM integration expert") == "ai"

    def test_ai_engineer_title(self):
        assert _detect_stack_hint("Hiring AI engineer to join our team") == "ai"

    def test_ai_agentic(self):
        assert _detect_stack_hint("Experience with agentic workflows required") == "ai"

    def test_ai_openai(self):
        assert _detect_stack_hint("Experience with OpenAI API and prompt engineering") == "ai"

    def test_ai_takes_priority_over_angular(self):
        assert _detect_stack_hint("Angular developer with LLM integration experience") == "ai"

    # Fullstack Angular + NestJS
    def test_nestjs_alone_routes_to_angular_nest(self):
        assert _detect_stack_hint("NestJS backend with Angular frontend") == "fullstack_angular_nest"

    def test_nestjs_with_angular_routes_to_angular_nest(self):
        assert _detect_stack_hint("We use Angular and NestJS") == "fullstack_angular_nest"

    def test_nest_js_dot_variant(self):
        assert _detect_stack_hint("Nest.js experience required") == "fullstack_angular_nest"

    # Fullstack React + Next.js
    def test_nextjs_routes_to_react_next(self):
        assert _detect_stack_hint("Next.js full-stack developer") == "fullstack_react_next"

    def test_nextjs_variant(self):
        assert _detect_stack_hint("nextjs and nodejs backend") == "fullstack_react_next"

    def test_nestjs_with_react_no_angular_routes_to_react_next(self):
        assert _detect_stack_hint("React frontend with NestJS backend") == "fullstack_react_next"

    def test_nestjs_with_both_react_and_angular_routes_to_angular_nest(self):
        # Angular present → angular_nest wins
        assert _detect_stack_hint("Angular or React frontend, NestJS backend") == "fullstack_angular_nest"

    # Angular track
    def test_angular_only(self):
        assert _detect_stack_hint("Senior Angular developer position") == "angular"

    def test_angular_with_rxjs(self):
        assert _detect_stack_hint("Angular, RxJS, NgRx required") == "angular"

    # React track
    def test_react_only(self):
        assert _detect_stack_hint("React developer with TypeScript") == "react"

    def test_react_hooks(self):
        assert _detect_stack_hint("Experience with React hooks and Redux") == "react"

    # JavaScript fallback
    def test_javascript_fallback(self):
        assert _detect_stack_hint("Frontend developer with JavaScript and TypeScript") == "javascript"

    def test_empty_text_fallback(self):
        assert _detect_stack_hint("") == "javascript"

    def test_unrelated_text_fallback(self):
        assert _detect_stack_hint("We are looking for a backend Java developer") == "javascript"

    # Case insensitivity
    def test_uppercase_angular(self):
        assert _detect_stack_hint("ANGULAR developer needed") == "angular"

    def test_mixed_case_nestjs(self):
        assert _detect_stack_hint("Experience with NestJS required") == "fullstack_angular_nest"


# ---------------------------------------------------------------------------
# _load_base_cv
# ---------------------------------------------------------------------------

class TestLoadBaseCv:

    def test_returns_empty_for_unknown_stack(self):
        assert _load_base_cv("python") == ""

    def test_returns_empty_for_empty_string(self):
        assert _load_base_cv("") == ""

    def test_loads_angular_base_cv(self, tmp_path):
        fake_content = "# Base CV Angular\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_angular.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("angular")
        assert result == fake_content

    def test_loads_react_base_cv(self, tmp_path):
        fake_content = "# Base CV React\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_react.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("react")
        assert result == fake_content

    def test_javascript_uses_react_file(self, tmp_path):
        fake_content = "# Base CV React/JS\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_react.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("javascript")
        assert result == fake_content

    def test_loads_ai_base_cv(self, tmp_path):
        fake_content = "# Base CV AI\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_ai.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("ai")
        assert result == fake_content

    def test_loads_fullstack_angular_nest(self, tmp_path):
        fake_content = "# Base CV Angular+Nest\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_fullstack_angular_nest.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("fullstack_angular_nest")
        assert result == fake_content

    def test_loads_fullstack_react_next(self, tmp_path):
        fake_content = "# Base CV React+Next\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_fullstack_react_next.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("fullstack_react_next")
        assert result == fake_content

    def test_returns_empty_when_file_missing(self, tmp_path, capsys):
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            result = _load_base_cv("angular")
        assert result == ""
        captured = capsys.readouterr()
        assert "Warning" in captured.out

    def test_case_insensitive_key(self, tmp_path):
        fake_content = "# Base CV Angular\n..."
        with patch("hunter.apply_api.PROMPTS_DIR", tmp_path):
            (tmp_path / "base_cv_angular.md").write_text(fake_content, encoding="utf-8")
            result = _load_base_cv("Angular")  # uppercase
        assert result == fake_content
