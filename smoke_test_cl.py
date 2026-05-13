"""
Smoke test: generate cover letter for a given job_posting.txt, run review loop,
print word count, metric count, gate results, and the final cover_letter_en.

Usage:
  python smoke_test_cl.py <path/to/job_posting.txt> [label]
"""
import sys, json, re
sys.stdout.reconfigure(encoding="utf-8")
from pathlib import Path

# ── bootstrap hunter config (loads .env) ─────────────────────────────────────
from hunter.config import LLM_PROVIDER, LLM_MODEL, LLM_API_KEY, APPLY_USE_CLI

if not LLM_API_KEY:
    print("ERROR: No LLM_API_KEY found in .env")
    sys.exit(1)

from llm_client import call_llm

SYSTEM_PROMPT = (Path(__file__).parent / "prompts" / "system_prompt.md").read_text(encoding="utf-8")
CANDIDATE = (Path(__file__).parent / "prompts" / "candidate_profile.md").read_text(encoding="utf-8")


def generate_content(job_text: str) -> dict:
    user_msg = f"## Candidate Profile\n\n{CANDIDATE}\n\n---\n\n## Job Posting\n\n{job_text}"
    return call_llm(
        system_prompt=SYSTEM_PROMPT,
        user_message=user_msg,
        provider=LLM_PROVIDER,
        model=LLM_MODEL,
        api_key=LLM_API_KEY,
        max_tokens=8000,
    )


def count_words(text: str) -> int:
    return len(text.split()) if text else 0


METRIC_RE = re.compile(
    r"\b\d+\s*%"
    r"|\b\d{3,}\b"
    r"|\b\d+\s*(?:x\b|\+\b)"
    r"|\b\d+\+?\s*(?:people|developers?|engineers?|banks?|apps?|applications?|"
    r"clients?|members?|months?|weeks?|hours?|microservices?|services?|projects?|"
    r"repos?|repositories?|teams?|companies|countries)\b",
    re.IGNORECASE,
)

BANNED_BODY = [
    re.compile(r"\baligns?\s+seamlessly\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+(?:perfectly\s+)?with\s+my\s+background\b", re.IGNORECASE),
    re.compile(r"\baligns?\s+perfectly\s+with\b", re.IGNORECASE),
    re.compile(r"\btechnical\s+acumen\b", re.IGNORECASE),
    re.compile(r"\bpassionate\s+about\b", re.IGNORECASE),
    re.compile(r"\bthrilled\s+to\b", re.IGNORECASE),
    re.compile(r"\bexcited\s+to\b", re.IGNORECASE),
    re.compile(r"\bproven\s+track\s+record\b", re.IGNORECASE),
    re.compile(r"\bcomfortable\s+owning\b", re.IGNORECASE),
    re.compile(r"\bcomfortable\s+with\b", re.IGNORECASE),
    re.compile(r"\bseamlessly\b", re.IGNORECASE),
    re.compile(r"\bsynergy\b", re.IGNORECASE),
    re.compile(r"\bleverage\b", re.IGNORECASE),
    re.compile(r"\bperfect\s+(?:fit|match)\b", re.IGNORECASE),
    re.compile(r"\bideal\s+match\b", re.IGNORECASE),
]

BANNED_CTA = [
    re.compile(r"I would welcome the opportunity to contribute", re.IGNORECASE),
    re.compile(r"Please find my CV attached", re.IGNORECASE),
    re.compile(r"Feel free to reach out", re.IGNORECASE),
    re.compile(r"I look forward to hearing from you", re.IGNORECASE),
    re.compile(r"Thank you for considering my application", re.IGNORECASE),
]

BANNED_OPENERS = [
    re.compile(r"^\s*Working with\s+\w.{0,60}for the past\s+\w.{0,40}I (?:have seen|learned|observed|know)\b", re.IGNORECASE),
    re.compile(r"^\s*Having\s+\w.{0,30}for\s+\d+\s+years?\b", re.IGNORECASE),
    re.compile(r"\bexactly the challenges you[''']?re\s+(?:facing|tackling|solving)\b", re.IGNORECASE),
    re.compile(r"\bis exactly what\s+.{1,80}?(?:requires|needs|is looking for)\b", re.IGNORECASE),
]


def audit_letter(letter: str, label: str = "") -> dict:
    wc = count_words(letter)
    cleaned = re.sub(r"\b10\+\s*years?\b", "", letter, flags=re.IGNORECASE)
    metrics = METRIC_RE.findall(cleaned)
    metric_count = len(metrics)

    last_para = letter.strip().split("\n\n")[-1] if "\n\n" in letter else letter.strip()
    cta_hits = [p.pattern for p in BANNED_CTA if p.search(last_para)]
    body_hits = [p.pattern for p in BANNED_BODY if p.search(letter)]
    opener = letter.strip()[:250]
    opener_hits = [p.pattern for p in BANNED_OPENERS if p.search(opener)]

    print(f"\n{'='*60}")
    print(f"  {label or 'COVER LETTER AUDIT'}")
    print(f"{'='*60}")
    print(f"  Word count  : {wc}  (target 220-280) {'PASS' if 220 <= wc <= 280 else 'FAIL'}")
    print(f"  Metrics     : {metric_count}  ({metrics})  {'PASS' if metric_count >= 2 else 'FAIL'}")
    print(f"  Opener bans : {'PASS' if not opener_hits else 'FAIL — ' + opener_hits[0][:60]}")
    print(f"  Body bans   : {'PASS' if not body_hits else 'FAIL — ' + body_hits[0][:60]}")
    print(f"  CTA bans    : {'PASS' if not cta_hits else 'FAIL — ' + cta_hits[0][:60]}")
    print()
    print("--- LETTER (EN) ---")
    print(letter.replace("\\n", "\n"))
    return {
        "wc": wc, "metrics": metric_count,
        "opener_ok": not opener_hits, "body_ok": not body_hits, "cta_ok": not cta_hits,
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python smoke_test_cl.py <job_posting.txt> [label]")
        sys.exit(1)

    posting_path = Path(sys.argv[1])
    label = sys.argv[2] if len(sys.argv) > 2 else posting_path.parent.name
    job_text = posting_path.read_text(encoding="utf-8")

    print(f"\n[smoke] Generating content for: {label}")
    print("[smoke] Calling LLM (this takes ~30-60s)...")
    content = generate_content(job_text)

    cl_en = content.get("cover_letter_en", "")
    company = content.get("company_name", "?")
    ats = content.get("ats_score", "?")
    print(f"[smoke] company={company}  ats={ats}%  stack={content.get('stack','?')}")

    audit_label = f"{label}  |  {company}  |  ATS:{ats}%"
    results = audit_letter(cl_en, audit_label)

    print("\n--- COVER LETTER (PL) ---")
    print((content.get("cover_letter_pl") or "").replace("\\n", "\n"))

    # run the actual apply_agent review loop
    print("\n[smoke] Running apply_agent review loop...")
    sys.path.insert(0, str(Path(__file__).parent))
    from apply_agent import _cover_letter_review_loop
    reviewed = _cover_letter_review_loop(content)
    reviewed_cl = reviewed.get("cover_letter_en", cl_en)

    if reviewed_cl != cl_en:
        print("\n[smoke] *** Letter was REWRITTEN by review loop ***")
        final_results = audit_letter(reviewed_cl, f"AFTER REVIEW — {label}")
    else:
        print("\n[smoke] Review loop: letter accepted as-is (score > 6)")
        final_results = results

    all_pass = all([
        220 <= final_results["wc"] <= 280,
        final_results["metrics"] >= 2,
        final_results["opener_ok"],
        final_results["body_ok"],
        final_results["cta_ok"],
    ])
    print(f"\n[smoke] OVERALL (final letter): {'PASS' if all_pass else 'FAIL (see above)'}")


if __name__ == "__main__":
    main()
