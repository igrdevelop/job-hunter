"""Tests for hunter/failure_signature.py (docs/APPLY_FAILURE_QUEUES_PLAN.md M0/M2)."""

from __future__ import annotations

from hunter.failure_signature import EMPTY, UNINFORMATIVE, signature, signature_id

# Real stderr of the 2026-09-10..21 CLI-argv incident (two vacancies), as it
# reached the owner's Telegram: every word of the prompt became a "deny rule".
INCIDENT_A = (
    'Permission deny rule "/apply" matches no known tool — check for typos.\n'
    'Permission deny rule "URL:" matches no known tool — check for typos.\n'
    'Permission deny rule "https://theprotocol.it/szczegoly/praca/'
    'ai-engineer-with-frontend-react-angular-warszawa" matches no known tool — check for typos.\n'
    'Permission deny rule "oferta" matches no known tool — check for typos.\n'
    'Permission deny rule "01000000-8d24-1cc1-b029-08df17e3651d\n\nJob" matches no known tool'
    " — check for typos.\n"
    'Permission deny rule "posting" matches no known tool — check for typos.\n'
    'Permission deny rule "with" matches no known tool —'
)
INCIDENT_B = (
    'Permission deny rule "/apply" matches no known tool — check for typos.\n'
    'Permission deny rule "URL:" matches no known tool — check for typos.\n'
    'Permission deny rule "https://www.linkedin.com/jobs/view/4455428397" matches no known tool'
    " — check for typos.\n"
    'Permission deny rule "Job" matches no known tool — check for typos.\n'
    'Permission deny rule "posting" matches no kn'
)


class TestSignature:
    def test_one_defect_one_signature_across_vacancies(self) -> None:
        # Different URLs, different truncation points — same defect.
        assert signature(INCIDENT_A) == signature(INCIDENT_B)
        assert "matches no known tool" in signature(INCIDENT_A)

    def test_signature_carries_no_vacancy_detail(self) -> None:
        sig = signature(INCIDENT_A)
        assert "theprotocol" not in sig
        assert "01000000" not in sig
        assert "/apply" not in sig

    def test_numbers_urls_and_paths_are_normalised(self) -> None:
        a = signature(
            "ERROR: fetch failed for https://a.example/job/1 (HTTP 403) at /app/x/y.py:12"
        )
        b = signature("ERROR: fetch failed for https://b.example/j/99 (HTTP 404) at /app/z/w.py:7")
        assert a == b

    def test_stdout_head_uses_the_error_line_not_the_first_line(self) -> None:
        text = (
            "[apply_agent] Step 1: fetching https://x.example/1\n"
            "[apply_agent] Step 1.5a: expired check ok\n"
            "[apply_agent] LLM ERROR: Invalid JSON in response after 3 attempts\n"
        )
        assert "LLM ERROR" in signature(text)

    def test_traceback_picks_the_exception_line(self) -> None:
        text = (
            "Traceback (most recent call last):\n"
            '  File "/app/apply_agent.py", line 12, in <module>\n'
            "ModuleNotFoundError: No module named 'hunter.foo'\n"
        )
        assert signature(text).startswith("ModuleNotFoundError")

    def test_different_defects_get_different_signatures(self) -> None:
        assert signature("ModuleNotFoundError: No module named 'x'") != signature(
            "ERROR: fetch failed (HTTP 403)"
        )

    def test_empty_and_uninformative(self) -> None:
        assert signature(None) == EMPTY
        assert signature("   ") == EMPTY
        assert signature("[apply_agent] Step 1: fetching\n[apply_agent] Step 2") == UNINFORMATIVE

    def test_signature_is_one_line_and_capped(self) -> None:
        sig = signature("ERROR: " + "x" * 1000)
        assert "\n" not in sig
        assert len(sig) <= 160

    def test_signature_id_is_stable_and_short(self) -> None:
        sig = signature(INCIDENT_A)
        assert signature_id(sig) == signature_id(signature(INCIDENT_B))
        assert len(signature_id(sig)) == 8
