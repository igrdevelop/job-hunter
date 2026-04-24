#!/usr/bin/env python3
"""Generate a sample cover letter (EN+PL) using current prompts — no Applications/ changes.

Run from repo root (requires LLM_API_KEY / ANTHROPIC_API_KEY):

  python tools/generate_sample_classic_cover.py

Output: tools/output/sample_cover_classic_en.txt and _pl.txt
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR))

from hunter.config import LLM_API_KEY, LLM_MODEL, LLM_PROVIDER  # noqa: E402
from llm_client import LLMError, call_llm  # noqa: E402


def _system_prompt() -> str:
    instructions = (PROJECT_DIR / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
    profile = PROJECT_DIR / "prompts" / "candidate_profile.md"
    if profile.exists():
        return profile.read_text(encoding="utf-8") + "\n\n---\n\n" + instructions
    return instructions


def main() -> None:
    fixture = PROJECT_DIR / "tests" / "fixtures" / "sample_job_posting_senior_fe.txt"
    if not fixture.is_file():
        print(f"Missing fixture: {fixture}")
        sys.exit(1)
    job_text = fixture.read_text(encoding="utf-8")

    if not LLM_API_KEY:
        print("Set LLM_API_KEY or ANTHROPIC_API_KEY in .env")
        sys.exit(1)

    user_message = (
        "Generate ONLY the cover letters for this job posting.\n\n"
        "Return a JSON object with exactly two string keys:\n"
        '  "cover_letter_en"\n  "cover_letter_pl"\n\n'
        "Follow ALL Cover Letter EN and Cover Letter PL rules in your instructions.\n\n"
        f"Job posting:\n{job_text}"
    )

    print(f"Calling {LLM_PROVIDER}/{LLM_MODEL}…")
    try:
        result = call_llm(
            system_prompt=_system_prompt(),
            user_message=user_message,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=8192,
        )
    except LLMError as e:
        print(f"LLM error: {e}")
        sys.exit(1)

    en = result.get("cover_letter_en", "")
    pl = result.get("cover_letter_pl", "")
    if not isinstance(en, str) or len(en.strip()) < 50:
        print("Invalid cover_letter_en in response")
        sys.exit(1)

    out_dir = PROJECT_DIR / "tools" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sample_cover_classic_en.txt").write_text(en.strip(), encoding="utf-8")
    if isinstance(pl, str) and len(pl.strip()) > 50:
        (out_dir / "sample_cover_classic_pl.txt").write_text(pl.strip(), encoding="utf-8")

    combined = {"cover_letter_en": en.strip(), "cover_letter_pl": (pl or "").strip()}
    (out_dir / "sample_cover_classic.json").write_text(
        json.dumps(combined, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Wrote {out_dir / 'sample_cover_classic_en.txt'}")
    if combined["cover_letter_pl"]:
        print(f"Wrote {out_dir / 'sample_cover_classic_pl.txt'}")
    print(f"Wrote {out_dir / 'sample_cover_classic.json'}")


if __name__ == "__main__":
    main()
