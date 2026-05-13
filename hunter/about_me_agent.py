"""
hunter/about_me_agent.py — Focused About Me generator.

Reads job_posting.txt + candidate_profile.md + few-shot examples,
calls LLM with a short focused prompt, saves About_Me_{LANG}.txt.
"""

import json
import logging
from pathlib import Path

from hunter.config import LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, PROJECT_DIR

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are writing a professional \"About Me\" / self-introduction for a senior developer's "
    "job application. Return ONLY valid JSON: {\"about_me\": \"<text>\"}. "
    "No markdown fences, no extra keys. "
    "IMPORTANT: Never use em dashes or en dashes. Use only regular hyphens."
)

_BANNED_EN = (
    "proven track record, passionate about, excited to, thrilled to, leverage, "
    "aligns with my background, seamlessly, comfortable with, perfect fit, ideal match"
)
_BANNED_PL = (
    "udowodniony track record, pasjonat, idealnym kandydatem, doskonale wpisuje się"
)


def generate_about_me(folder: Path, lang: str) -> str:
    """Generate About Me text for a job application folder.

    Args:
        folder: Path to Applications/{date}/{Company}/ — must contain job_posting.txt
        lang:   "en" or "pl"

    Returns:
        Generated About Me text (plain string, ready to write to file / send to Telegram).
        Returns "" on failure (logs error, does not raise).

    Side effect:
        Saves About_Me_EN.txt or About_Me_PL.txt inside folder.
    """
    lang = lang.lower()
    if lang not in ("en", "pl"):
        logger.error(f"[about_me_agent] Invalid lang '{lang}', must be 'en' or 'pl'")
        return ""

    job_posting_path = folder / "job_posting.txt"
    if not job_posting_path.exists():
        logger.warning(f"[about_me_agent] job_posting.txt missing in {folder}")
        return ""

    job_text = job_posting_path.read_text(encoding="utf-8", errors="replace")[:2000]

    # Optional: extract already-parsed fields from content.json
    company_name = ""
    stack = ""
    job_title = ""
    content_path = folder / "content.json"
    if content_path.exists():
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
            company_name = content.get("company_name", "")
            stack = content.get("stack", "")
            job_title = content.get("job_title", "")
        except Exception:
            pass

    # Load candidate profile
    profile_path = PROJECT_DIR / "prompts" / "candidate_profile.md"
    try:
        candidate_profile = profile_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"[about_me_agent] Cannot read candidate_profile.md: {e}")
        return ""

    # Load few-shot examples
    example_file = f"about_me_{lang}.md"
    examples_path = PROJECT_DIR / "prompts" / "examples" / example_file
    try:
        examples = examples_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning(f"[about_me_agent] Cannot read {example_file}: {e}")
        examples = ""

    lang_label = "ENGLISH" if lang == "en" else "POLISH"
    banned = _BANNED_EN if lang == "en" else _BANNED_PL
    pl_note = "\n{PL only}: Write natively in Polish - NOT a translation. Use Polish idioms." if lang == "pl" else ""

    job_meta = ""
    if company_name or job_title or stack:
        parts = []
        if company_name:
            parts.append(f"Company: {company_name}")
        if job_title:
            parts.append(f"Title: {job_title}")
        if stack:
            parts.append(f"Stack: {stack}")
        job_meta = "\n".join(parts) + "\n\n"

    user_message = f"""## Candidate Profile
{candidate_profile}

---

## Target Job
{job_meta}{job_text}

---

## Examples of good About Me texts ({lang.upper()})
{examples}

---

## Task
Write About Me in {lang_label} tailored to THIS job.
Length: 4-6 sentences (~100-150 words).
Lead with: seniority + stack match to this role.
Include: at least 1 quantified metric from experience.
Highlight: what is MOST relevant to this specific role (match to must-haves).
{pl_note}

BANNED phrases: {banned}
"""

    try:
        from llm_client import call_llm, LLMError
        result = call_llm(
            system_prompt=_SYSTEM_PROMPT,
            user_message=user_message,
            provider=LLM_PROVIDER,
            model=LLM_MODEL,
            api_key=LLM_API_KEY,
            max_tokens=1024,
        )
    except Exception as e:
        logger.error(f"[about_me_agent] LLM call failed: {e}")
        return ""

    # Parse result
    text = ""
    if isinstance(result, dict):
        text = result.get("about_me", "")
        if not text:
            # Fallback: try raw string if LLM returned something unexpected
            text = str(result)
    else:
        text = str(result)

    if not text:
        logger.error("[about_me_agent] Empty result from LLM")
        return ""

    # Save to file
    out_filename = f"About_Me_{lang.upper()}.txt"
    out_path = folder / out_filename
    try:
        out_path.write_text(text, encoding="utf-8")
        print(f"[about_me_agent] Saved {out_filename}")
    except Exception as e:
        logger.error(f"[about_me_agent] Failed to save {out_filename}: {e}")

    return text
