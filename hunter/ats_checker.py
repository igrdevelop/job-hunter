"""
hunter/ats_checker.py — Independent ATS scoring agent.

Mimics how real ATS systems (Workday, Greenhouse, Lever, iCIMS) score resumes:
  1. Keyword extraction + exact/section-weighted match  (dominant signal, no API)
  2. TF-IDF cosine similarity via scikit-learn           (semantic signal, no API)
  3. LLM independent gap analysis                        (context-aware, separate call)

The LLM reviewer does NOT know it generated the resume — eliminates self-assessment bias.

Weights (when LLM review is available):
  keyword match  60%
  LLM reviewer   30%
  TF-IDF         10%  (informational; cosine between job/resume is structurally low)

Without LLM (api_key empty or run_llm_review=False):
  keyword match  75%
  TF-IDF         25%
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Keyword patterns ──────────────────────────────────────────────────────────

_TECH_RE = re.compile(
    r"\b("
    r"Angular|React|Vue\.?js|Svelte|Next\.?js|Nuxt|Remix|Solid\.?js|"
    r"TypeScript|JavaScript|Python|Java|Kotlin|Swift|Go|Rust|C#|\.NET|PHP|Ruby|"
    r"RxJS|NgRx|Redux|MobX|Zustand|Pinia|Signals|"
    r"Node\.?js|Express|NestJS|FastAPI|Django|Flask|Spring|Laravel|"
    r"GraphQL|REST(?:ful)?|gRPC|WebSocket|tRPC|"
    r"HTML5?|CSS3?|SCSS|Sass|Tailwind|Material(?:\s+UI)?|Bootstrap|Ant\s+Design|"
    r"Jest|Cypress|Playwright|Karma|Jasmine|Vitest|Testing\s+Library|"
    r"Docker|Kubernetes|AWS|Azure|GCP|Terraform|Ansible|"
    r"CI/?CD|GitHub\s+Actions|GitLab\s+CI|Jenkins|CircleCI|"
    r"PostgreSQL|MySQL|MongoDB|Redis|Elasticsearch|DynamoDB|Firestore|"
    r"Git|Agile|Scrum|Kanban|TDD|BDD|"
    r"Micro[- ]?frontends?|Web[- ]?components?|PWA|SPA|SSR|SSG|"
    r"Webpack|Vite|Rollup|Babel|ESLint|Prettier|"
    r"Storybook|Nx|Turbo[- ]?repo|Monorepo|"
    r"OAuth|JWT|SSO|RBAC|SAML|"
    r"Figma|Zeplin|Sketch|"
    r"Accessibility|a11y|WCAG|i18n|l10n|"
    r"Performance\s+optimization|Web\s+Vitals|Lighthouse"
    r")\b",
    re.IGNORECASE,
)

_SOFT_RE = re.compile(
    r"\b("
    r"communication|collaboration|mentoring|coaching|leadership|"
    r"problem[- ]solving|analytical|ownership|proactive|initiative|"
    r"cross[- ]functional|stakeholder|"
    r"english|german|polish|french|spanish"
    r")\b",
    re.IGNORECASE,
)

_REQUIRED_SECTION_RE = re.compile(
    r"(?:requirements?|must[- ]have|required|mandatory|you\s+(?:will\s+)?need|"
    r"what\s+we(?:'re|\s+are)\s+looking\s+for|key\s+qualifications?)"
    r"[^\n]{0,60}\n([\s\S]{0,800}?)(?:\n\n|\Z)",
    re.IGNORECASE,
)


def _extract_keywords(job_text: str) -> list[str]:
    """Extract deduplicated tech + soft keywords from job posting."""
    # Boost keywords found in required/must-have sections
    priority_text = ""
    for m in _REQUIRED_SECTION_RE.finditer(job_text):
        priority_text += m.group(1) + " "

    seen: dict[str, int] = {}  # lower → priority (2=required, 1=mentioned)
    for kw in _TECH_RE.findall(priority_text) + _SOFT_RE.findall(priority_text):
        key = kw.lower()
        seen[key] = max(seen.get(key, 0), 2)
    for kw in _TECH_RE.findall(job_text) + _SOFT_RE.findall(job_text):
        key = kw.lower()
        if key not in seen:
            seen[key] = 1

    # Return highest-priority first; preserve original casing from first match
    casing: dict[str, str] = {}
    for kw in _TECH_RE.findall(job_text) + _SOFT_RE.findall(job_text):
        if kw.lower() not in casing:
            casing[kw.lower()] = kw

    return [casing[k] for k in sorted(seen, key=lambda k: -seen[k])]


def _skills_section(resume_text: str) -> str:
    """Extract the skills section text (first ~600 chars after a skills header)."""
    m = re.search(
        r"(?:skills?|technologies?|tech\s+stack)[^\n]{0,40}\n([\s\S]{0,600}?)(?:\n\n|\Z)",
        resume_text,
        re.IGNORECASE,
    )
    return m.group(1).lower() if m else ""


def _keyword_match_score(
    keywords: list[str], resume_text: str
) -> tuple[float, list[str], list[str]]:
    """
    Exact match with section weighting.
    Keywords found in the Skills section count 1.5×.
    Returns (score 0–1, matched, missing).
    """
    if not keywords:
        return 1.0, [], []

    resume_lower = resume_text.lower()
    skills_text = _skills_section(resume_text)

    matched, missing = [], []
    weighted, total = 0.0, 0.0

    for kw in keywords:
        pattern = re.escape(kw.lower())
        in_resume = bool(re.search(pattern, resume_lower))
        in_skills = bool(re.search(pattern, skills_text))
        w = 1.5 if in_skills else 1.0
        total += w
        if in_resume:
            matched.append(kw)
            weighted += w
        else:
            missing.append(kw)

    return (weighted / total) if total else 0.0, matched, missing


def _tfidf_score(job_text: str, resume_text: str) -> float:
    """
    TF-IDF cosine similarity between job posting and resume.
    Returns 0–1. Typical good match: 0.15–0.40 (docs are structurally different).
    We normalise against 0.35 as "full score" so a strong match maps near 100%.
    """
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), min_df=1)
        mat = vec.fit_transform([job_text, resume_text])
        raw = float(cosine_similarity(mat[0:1], mat[1:2])[0][0])
        return min(raw / 0.35, 1.0)
    except ImportError:
        print("[ats_checker] scikit-learn not installed — TF-IDF signal skipped")
        return 0.5
    except Exception:
        return 0.5


# ── LLM independent reviewer ──────────────────────────────────────────────────

_LLM_SYSTEM = (
    "You are a strict Applicant Tracking System (ATS) evaluating how well a resume "
    "matches a job posting. You did NOT write this resume. Your role is to find gaps "
    "and score objectively. Respond with JSON only — no markdown fences."
)

_LLM_PROMPT = """\
Job posting:
{job_text}

Resume:
{resume_text}

Evaluate the resume against the job posting strictly as an ATS would.

Check:
1. Required and preferred skills: which are present, which are absent
2. Job title and seniority alignment
3. Years of experience match
4. Keyword density in Skills section vs body
5. Language requirements (English level, other languages)
6. Missing certifications or education requirements

Score 0–100 where:
  90–100 = nearly all required keywords present, strong alignment
  75–89  = most required keywords, minor gaps
  60–74  = several required keywords missing
  below 60 = significant gaps

Return JSON only:
{{"ats_score": <int 0-100>, "missing_keywords": [<strings>], "recommendations": [<up to 5 short actionable strings>], "gap_report": "<2-3 sentences summarising main gaps>"}}"""


def _llm_review(
    job_text: str,
    resume_text: str,
    provider: str,
    model: str,
    api_key: str,
) -> tuple[float, list[str], list[str], str]:
    """Returns (score 0–100, missing_keywords, recommendations, gap_report)."""
    try:
        from llm_client import LLMError, call_llm

        result = call_llm(
            system_prompt=_LLM_SYSTEM,
            user_message=_LLM_PROMPT.format(
                job_text=job_text[:4000],
                resume_text=resume_text[:3000],
            ),
            provider=provider,
            model=model,
            api_key=api_key,
            max_tokens=800,
        )
        score = max(0.0, min(100.0, float(result.get("ats_score", 50))))
        return (
            score,
            result.get("missing_keywords", []),
            result.get("recommendations", []),
            result.get("gap_report", ""),
        )
    except Exception as e:
        print(f"[ats_checker] LLM review error: {e}")
        return -1.0, [], [], ""


# ── Public API ────────────────────────────────────────────────────────────────

@dataclass
class ATSResult:
    score: float
    keyword_score: float
    semantic_score: float
    llm_score: float  # -1 if skipped
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    recommendations: list[str] = field(default_factory=list)
    llm_gap_report: str = ""

    def passed(self, threshold: float = 95.0) -> bool:
        return self.score >= threshold

    def summary(self) -> str:
        status = "PASS" if self.passed() else "BELOW THRESHOLD"
        lines = [
            f"ATS score: {self.score:.1f}%  [{status}]",
            f"  keyword match : {self.keyword_score:.1f}%  "
            f"({len(self.matched_keywords)} matched, {len(self.missing_keywords)} missing)",
            f"  TF-IDF cosine : {self.semantic_score:.1f}%",
        ]
        if self.llm_score >= 0:
            lines.append(f"  LLM reviewer  : {self.llm_score:.1f}%")
        if self.missing_keywords:
            top = self.missing_keywords[:10]
            lines.append(f"  Missing       : {', '.join(top)}")
        if self.llm_gap_report:
            lines.append(f"  Gap report    : {self.llm_gap_report}")
        return "\n".join(lines)

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "keyword_score": self.keyword_score,
            "semantic_score": self.semantic_score,
            "llm_score": self.llm_score,
            "matched_keywords": self.matched_keywords,
            "missing_keywords": self.missing_keywords,
            "recommendations": self.recommendations,
            "llm_gap_report": self.llm_gap_report,
        }


def check(
    job_text: str,
    resume_text: str,
    provider: str = "",
    model: str = "",
    api_key: str = "",
    run_llm_review: bool = True,
) -> ATSResult:
    """
    Run full ATS check and return ATSResult.

    Weights (with LLM):   keyword 60% + LLM 30% + TF-IDF 10%
    Weights (no LLM):     keyword 75%            + TF-IDF 25%
    """
    keywords = _extract_keywords(job_text)
    kw_raw, matched, missing = _keyword_match_score(keywords, resume_text)
    keyword_score = kw_raw * 100.0

    tfidf_raw = _tfidf_score(job_text, resume_text)
    semantic_score = tfidf_raw * 100.0

    llm_score = -1.0
    llm_missing: list[str] = []
    llm_recs: list[str] = []
    gap_report = ""

    if run_llm_review and api_key:
        llm_score, llm_missing, llm_recs, gap_report = _llm_review(
            job_text, resume_text, provider, model, api_key
        )

    # Merge missing keyword lists (dedup, keyword-matcher list first)
    seen_missing = {k.lower() for k in missing}
    extra_missing = [k for k in llm_missing if k.lower() not in seen_missing]
    all_missing = missing + extra_missing

    # Recommendations
    recs: list[str] = []
    if missing:
        recs.append(f"Add to Skills section: {', '.join(missing[:8])}")
    recs.extend(llm_recs[:4])

    if llm_score >= 0:
        combined = keyword_score * 0.60 + llm_score * 0.30 + semantic_score * 0.10
    else:
        combined = keyword_score * 0.75 + semantic_score * 0.25

    return ATSResult(
        score=round(combined, 1),
        keyword_score=round(keyword_score, 1),
        semantic_score=round(semantic_score, 1),
        llm_score=round(llm_score, 1),
        matched_keywords=matched,
        missing_keywords=all_missing,
        recommendations=recs,
        llm_gap_report=gap_report,
    )
