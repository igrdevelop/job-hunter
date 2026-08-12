"""The identity gate: refuse to render a document without a configured identity.

Before this gate, a checkout with no candidate/candidate.yaml fell back to the
project owner's real name, phone and email — so a second person's very first
run produced a CV addressed from him, and nothing anywhere said so. Aborting is
the correct failure mode: an aborted apply is retried, a CV already mailed to an
employer under the wrong name is not.
"""

from __future__ import annotations

import textwrap

import pytest

from hunter import candidate


@pytest.fixture(autouse=True)
def _isolate_candidate_path():
    """Every test here picks its own yaml; never touch the real checkout's."""
    yield
    candidate._set_path(None)


def _write_yaml(tmp_path, body: str):
    path = tmp_path / "candidate.yaml"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    candidate._set_path(path)
    return path


def test_missing_file_reports_every_required_field(tmp_path):
    candidate._set_path(tmp_path / "does_not_exist.yaml")
    assert candidate.missing_identity_fields() == list(candidate.REQUIRED_IDENTITY_FIELDS)


def test_missing_file_blocks_generation(tmp_path):
    candidate._set_path(tmp_path / "does_not_exist.yaml")
    with pytest.raises(candidate.CandidateIdentityMissing) as exc:
        candidate.require_identity()
    message = str(exc.value)
    # The message has to be actionable on its own — it is what the operator
    # sees in the apply log, with no other context.
    assert "identity.full_name" in message
    assert "candidate.yaml.example" in message
    assert "SETUP_NEW_USER" in message


def test_complete_identity_passes(tmp_path):
    _write_yaml(
        tmp_path,
        """
        identity:
          full_name: "Jane Doe"
          contact: "+48 123 456 789 | jane@example.com | Warsaw, Poland"
          cv_filename_prefix: "Jane_Doe_CV"
        """,
    )
    assert candidate.missing_identity_fields() == []
    candidate.require_identity()  # must not raise


def test_partial_identity_names_only_the_missing_field(tmp_path):
    _write_yaml(
        tmp_path,
        """
        identity:
          full_name: "Jane Doe"
          cv_filename_prefix: "Jane_Doe_CV"
        """,
    )
    assert candidate.missing_identity_fields() == ["identity.contact"]
    with pytest.raises(candidate.CandidateIdentityMissing):
        candidate.require_identity()


def test_blank_value_counts_as_missing(tmp_path):
    """A present-but-empty name is no better than an absent one — it would
    render a CV with a blank name line rather than an obvious error."""
    _write_yaml(
        tmp_path,
        """
        identity:
          full_name: "   "
          contact: "+48 123 456 789"
          cv_filename_prefix: "Jane_Doe_CV"
        """,
    )
    assert candidate.missing_identity_fields() == ["identity.full_name"]


def test_optional_identity_fields_are_not_gated(tmp_path):
    """aka is optional by design (blank omits the CV subtitle) and headline has
    a generic non-personal default — neither may block a valid setup."""
    _write_yaml(
        tmp_path,
        """
        identity:
          full_name: "Jane Doe"
          contact: "+48 123 456 789"
          cv_filename_prefix: "Jane_Doe_CV"
        """,
    )
    candidate.require_identity()
    assert candidate.get("identity.aka", candidate.DEFAULT_AKA) == ""
    assert candidate.get("identity.headline", candidate.DEFAULT_HEADLINE) == "Software Developer"


def test_generate_docs_main_is_actually_wired_to_the_gate(tmp_path, monkeypatch, capsys):
    """The gate only protects anything if generate_docs.main() calls it.

    Passing a content.json path that does NOT exist is deliberate: main() reads
    that file immediately after the gate, so exiting with the identity error
    (rather than a FileNotFoundError) proves the gate runs FIRST — before any
    file is touched and long before a PDF could be rendered.
    """
    import generate_docs

    candidate._set_path(tmp_path / "does_not_exist.yaml")
    monkeypatch.setattr("sys.argv", ["generate_docs.py", str(tmp_path / "no_such_content.json")])

    with pytest.raises(SystemExit) as exc:
        generate_docs.main()

    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "identity is not configured" in out
    assert "identity.full_name" in out


def test_defaults_carry_no_real_identity():
    """The placeholders must be obviously non-functional. If someone ever
    'restores' a real value here, this fails alongside the readiness scan."""
    for value in (
        candidate.DEFAULT_FULL_NAME,
        candidate.DEFAULT_CV_FILENAME_PREFIX,
        candidate.DEFAULT_CONTACT,
        candidate.DEFAULT_HEADLINE,
    ):
        assert "@" not in value, f"{value!r} looks like a real email"
        assert "+48" not in value, f"{value!r} looks like a real phone number"
        assert "Petrasheuski" not in value
