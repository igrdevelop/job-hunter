"""Post-apply outreach draft — outreach.md next to the generated CV (issue #138).

Almost no cold ATS application gets a human reply; a short personal message to
the recruiter right after applying is the lever that does move replies. After
every successful apply (both pipelines) this module writes `outreach.md` into
the application folder (`Applications/{date}/{Company}/`) with:

- contact(s) parsed from the posting itself (hunter/contact_extract.py, $0),
- a ready-to-paste ≤300-char message (LinkedIn connection-note limit) in the
  posting's language, plus an EN version when the original is Polish — one
  cheap JUDGE-tier (Haiku) call, grounded ONLY in the already-judged
  content.json (no fresh fabrication surface).

No Telegram delivery, no Sheets changes (owner decisions 2026-07-10): the file
rides the existing Google Drive folder upload as-is. The bot NEVER sends the
message anywhere — the owner copies it and sends it from his own accounts.

Best-effort throughout: `run_outreach()` never raises and must never fail or
delay the apply (same contract as the Drive/Sheets mirroring).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from hunter.best_effort import best_effort
from hunter.config import (
    JUDGE_API_KEY,
    JUDGE_MODEL,
    JUDGE_PROVIDER,
    OUTREACH_ENABLED,
)
from hunter.contact_extract import Contact, extract_contacts

OUTREACH_FILENAME = "outreach.md"
MESSAGE_CHAR_LIMIT = 300  # LinkedIn connection-request note limit

_SYSTEM_PROMPT = """You ghost-write a short LinkedIn outreach note from a job candidate to the recruiter who posted a vacancy the candidate has just applied to.

Rules:
- STRICTLY under {limit} characters (LinkedIn connection-note limit). Count characters, not words.
- Write in {lang_name}. {extra_lang}
- Structure: one line who the candidate is, one concrete hook matched to the posting's stack, one small ask. Value first — never "I applied, please look at my CV".
- Use ONLY facts from the CANDIDATE SUMMARY below. Never invent skills, employers, or numbers.
- No greetings-with-name placeholders like "Dear [Name]" — start the message directly (LinkedIn shows names anyway).
- Plain text, no emoji, no hashtags.

Return JSON only: {{"message": "...", "message_en": "..."}}.
"message" is in {lang_name}; "message_en" is the English version (identical content). If {lang_name} is English, set "message_en" to null."""

_USER_TEMPLATE = """VACANCY: {title} at {company}
STACK: {stack}

CANDIDATE SUMMARY (the only permitted source of facts about the candidate):
{summary}

JOB POSTING (excerpt):
{job_excerpt}"""


def run_outreach(folder: Path, url: str = "") -> Path | None:
    """Write outreach.md into the application folder. Never raises."""
    result: Path | None = None
    with best_effort("outreach.run_outreach"):
        try:
            result = _run_outreach(Path(folder), url)
        except Exception as e:  # noqa: BLE001 — best-effort, never fail an apply
            print(f"[outreach] failed (continuing): {e}")
            raise
    return result


def _run_outreach(folder: Path, url: str) -> Path | None:
    if not OUTREACH_ENABLED:
        return None

    job_text = ""
    posting = folder / "job_posting.txt"
    if posting.exists():
        job_text = posting.read_text(encoding="utf-8", errors="replace")

    content: dict = {}
    content_path = folder / "content.json"
    if content_path.exists():
        try:
            content = json.loads(content_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            content = {}

    contacts = extract_contacts(job_text)
    lang = (content.get("primary_lang") or "").upper()
    if lang not in ("PL", "EN"):
        from hunter.lang_guard import detect_posting_language

        lang = detect_posting_language(job_text) if job_text else "EN"

    message, message_en = _draft_messages(content, job_text, lang)

    if not contacts and not message:
        print("[outreach] nothing to write (no contact found, message draft failed)")
        return None

    out_path = folder / OUTREACH_FILENAME
    out_path.write_text(
        _render(content, url, contacts, message, message_en, lang),
        encoding="utf-8",
    )
    print(
        f"[outreach] wrote {out_path.name} "
        f"({len(contacts)} contact(s), message: {'yes' if message else 'DRAFT FAILED'})"
    )
    return out_path


# ── Message drafting ──────────────────────────────────────────────────────────


def _flatten_skills(skills) -> list[str]:
    """resume_en.skills is a dict of category -> comma string (generate_docs.py's
    build_resume, claim_judge.iter_judged_fields) in real content.json — flatten
    it into individual skill items. A bare list is accepted too (defensive)."""
    items: list[str] = []
    if isinstance(skills, dict):
        for val in skills.values():
            if isinstance(val, str):
                items.extend(s.strip() for s in val.split(",") if s.strip())
            elif isinstance(val, list):
                items.extend(str(v).strip() for v in val if str(v).strip())
    elif isinstance(skills, list):
        items.extend(str(v).strip() for v in skills if str(v).strip())
    return items


def _candidate_summary(content: dict) -> str:
    resume = content.get("resume_en") or {}
    skill_items = _flatten_skills(resume.get("skills"))
    parts = [
        str(resume.get("summary") or ""),
        "Key skills: " + ", ".join(skill_items[:10]) if skill_items else "",
    ]
    return "\n".join(p for p in parts if p).strip()


def _draft_messages(content: dict, job_text: str, lang: str) -> tuple[str, str]:
    """One cheap judge-tier call → (message_in_posting_lang, message_en).

    message_en is "" for EN postings (the main message IS English) and on any
    failure. Both messages are "" when the call fails entirely — the caller
    still writes the contact block.
    """
    summary = _candidate_summary(content)
    if not summary:
        return "", ""

    lang_name = "Polish" if lang == "PL" else "English"
    extra = (
        "Also provide an English version in message_en."
        if lang == "PL"
        else "Set message_en to null."
    )
    try:
        from llm_client import call_llm

        raw = call_llm(
            system_prompt=_SYSTEM_PROMPT.format(
                limit=MESSAGE_CHAR_LIMIT, lang_name=lang_name, extra_lang=extra
            ),
            user_message=_USER_TEMPLATE.format(
                title=content.get("job_title") or "the role",
                company=content.get("company_name") or "the company",
                stack=content.get("stack") or "",
                summary=summary,
                job_excerpt=job_text[:1500],
            ),
            provider=JUDGE_PROVIDER,
            model=JUDGE_MODEL,
            api_key=JUDGE_API_KEY,
            max_tokens=512,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[outreach] message draft failed: {e}")
        return "", ""

    if not isinstance(raw, dict):
        return "", ""
    message = _clean(raw.get("message"))
    message_en = _clean(raw.get("message_en")) if lang == "PL" else ""
    return message, message_en


def _clean(value) -> str:
    if not isinstance(value, str):
        return ""
    msg = re.sub(r"\s+", " ", value).strip()
    # A cheap model occasionally overshoots the limit — trim at a word
    # boundary rather than shipping a note LinkedIn will reject.
    if len(msg) > MESSAGE_CHAR_LIMIT:
        msg = msg[:MESSAGE_CHAR_LIMIT]
        msg = msg[: msg.rfind(" ")].rstrip(" ,.;") + "…"
    return msg


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render(
    content: dict,
    url: str,
    contacts: list[Contact],
    message: str,
    message_en: str,
    lang: str,
) -> str:
    company = content.get("company_name") or "?"
    title = content.get("job_title") or "?"
    lines = [
        f"# Outreach — {company}",
        "",
        f"**Role:** {title}",
    ]
    # source_permalink (e.g. a captured LinkedIn Scout post permalink) is the
    # real, clickable link when `url` itself is only a synthetic dedup key —
    # prefer it so the owner has something to actually open and apply/message on.
    display_url = content.get("source_permalink") or url
    if display_url and not display_url.startswith("paste://"):
        lines.append(f"**Posting:** {display_url}")
    lines.append("")

    lines.append("## Contact")
    if contacts:
        for c in contacts:
            bits = [b for b in (c.name, c.email, c.phone) if b]
            lines.append(f"- **{' · '.join(bits)}**")
            if c.evidence:
                lines.append(f"  - from posting: “{c.evidence}”")
    else:
        lines.append(
            "- _No contact found in the posting — search LinkedIn for the "
            f"recruiter / hiring manager at {company}._"
        )
    lines.append("")

    if message:
        label = "PL" if lang == "PL" else "EN"
        lines += [
            f"## Message ({label}, ≤{MESSAGE_CHAR_LIMIT} chars — LinkedIn connection note)",
            "",
            "```",
            message,
            "```",
            "",
        ]
        if message_en:
            lines += ["## Message (EN)", "", "```", message_en, "```", ""]
    else:
        lines += ["## Message", "", "_Draft failed — write manually._", ""]

    lines.append("_Send manually from your own account. The bot never sends anything._")
    return "\n".join(lines) + "\n"
