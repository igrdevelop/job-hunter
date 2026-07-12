"""Job-matching filter rules (FILTER dict), split out of hunter/config.py.

Pure organizational split (no behavior change): FILTER was ~210 lines, a
third of config.py, and almost entirely regex patterns with calibration
comments explaining WHY each one exists — worth its own file, separate from
the env-var/schedule/toggle settings the rest of config.py holds. Re-exported
as `hunter.config.FILTER` for backward compatibility — every existing
`from hunter.config import FILTER` import keeps working unchanged.

To add/tune a rule: edit the relevant list/pattern below, then see
hunter/filters.py for where each key is consumed (title_keywords/
require_angular/exclude_levels/locations feed classify_job() directly;
exclude_body_disqualifiers/exclude_body_onsite_city/exclude_ai_training/etc.
gate the body-level checks). No env var reads live here — this is
static, code-reviewed policy, not a runtime toggle.
"""

# ── Job filters ───────────────────────────────────────────────────────────────
# Angular-only: title must match at least one keyword AND contain "angular"
# (unless it's a generic "frontend"/"typescript" title — require_angular catches those)
FILTER = {
    # Title must contain at least ONE of these (case-insensitive)
    "title_keywords": [
        "angular",
        "frontend",
        "front-end",
        "javascript",
        "typescript",
    ],

    # Angular not required in title — many Angular jobs are titled just "Frontend Developer"
    "require_angular": False,

    "exclude_levels": [
        "junior",
        "intern",
        "internship",
        "trainee",
        "stażysta",
        "praktykant",
        "staz",
        # P-8.1: management / leadership / non-IC roles
        "tech lead",
        "tech-lead",
        "techlead",
        "project lead",
        "engineering manager",
        "head of engineering",
        "vp of engineering",
        "cto",
        # Part-time — not relevant for full-time search
        "part-time",
        "part time",
        "parttime",
    ],

    "locations": [
        # Always accept: fully remote regardless of city
        "remote",
        "zdalnie",
        "zdalna",
        # Accept Wrocław (on-site OR hybrid — hybrid elsewhere is rejected)
        "wrocław",
        "wroclaw",
    ],

    # Title matching ANY regex → skip
    "exclude_patterns": [
        r"\bjava\b",
        r"\.net",
        # NOTE: trailing \b after "#" never matches ("#" is a non-word char, so
        # there is no word boundary between "#" and the following space). Use a
        # leading boundary only so "C#", "(C#", "C#/Angular" are all caught.
        r"\bc#",
        r"\bphp\b",
        r"\bqa\b",
        r"\bsdet\b",
        r"quality\s+assurance",
        r"test\s+automation",
        # fullstack WITHOUT angular is handled by _is_fullstack_without_angular()
        # in filters.py — we don't put it in exclude_patterns so Angular fullstack passes.
        r"\bbackend\b",
        r"\bback-end\b",
        r"\bvue\b",
        r"\bnuxt\b",
        r"\bmagento\b",
        r"\bruby\b",
        # P-3.3: React Native — mobile-only, not FE web
        r"\breact\s+native\b",
        r"\breact[- ]native\b",
        # P-4.1: eCommerce/CMS platforms — not web-FE stack
        r"\bhyv[äa]\b",           # Hyva (Magento theme) — Finnish spelling variants
        r"\badobe\s+commerce\b",   # Adobe Commerce = Magento rebranded
        r"\bpwa\s+studio\b",       # Magento PWA Studio
        r"\bshopware\b",
        r"\bshopify\b",
        r"\bbigcommerce\b",
        r"\bwoocommerce\b",
        r"\bdrupal\b",
        r"\bwordpress\b",
        r"\bsharepoint\b",
        r"\bsap\b",
        # P-7.1: Salesforce / DevOps / SRE / mobile / test-automation roles
        r"\bsalesforce\b",
        r"\bdevops\b",
        r"\bdev-ops\b",
        r"\bsre\b",                    # Site Reliability Engineer
        r"\bplatform\s+engineer\b",
        r"\bcloud\s+engineer\b",
        r"\binfrastructure\s+engineer\b",
        r"\bandroid\b",
        r"\bios\s+developer\b",
        r"\bswift\s+developer\b",
        r"\bkotlin\s+developer\b",
        r"\bflutter\b",
        r"\bautomation\s+engineer\b",
        r"\btesting\s+engineer\b",
        # P-8.1: management / non-IC roles (regex for mixed-case not caught by exclude_levels)
        r"\btech\s+lead\b",
        r"\bproject\s+lead\b",
        r"\bpart[- ]?time\b",
        # Low-code / non-web-FE platforms and niche roles the candidate skips
        r"\bmendix\b",
        r"\boutsystems\b",
        r"\blow[-\s]?code\b",
        r"\bemail\s+developer\b",
        r"\bui\s+designer\b",
        # AI data-labeling / "AI training" gig roles (not real FE engineering)
        r"\bai\s+train(?:ing|er)\b",
        r"\bai\s+tutor\b",
        r"\bdata\s+annotat\w*\b",
        r"\bdata\s+label(?:l)?ing\b",
    ],

    # Skip jobs that mention React but NOT Angular (React-only roles)
    "exclude_react_without_angular": True,

    # Fullstack policy: a "Full Stack / Fullstack" title with NO Angular is always
    # blocked (handled in filters._is_unwanted_fullstack). When Angular IS present
    # the role is blocked only if it is paired with a *heavy backend* stack below
    # (checked in title AND body). Node/Nuxt are deliberately NOT in this list, so a
    # JS/Node fullstack-with-Angular role still passes (per owner's preference).
    "exclude_fullstack_with_backend": True,
    "fullstack_backend_stacks": [
        r"\bjava\b",
        r"\bspring(?:\s+boot)?\b",
        r"\.net\b",
        r"\basp\.net\b",
        r"\bc#",
        r"\bpython\b",
        r"\bdjango\b",
        r"\bgolang\b",
        r"\bphp\b",
        r"\bruby\s+on\s+rails\b",
    ],

    # Disqualifiers hidden in the job BODY (title looks like clean FE, but the
    # description reveals a stack/platform the candidate doesn't want). Checked
    # against the full job text blob, mirroring the German/contract/relocation gates.
    "exclude_body_disqualifiers": True,
    "body_exclude_patterns": [
        r"\bblazor\b",
        r"\bmendix\b",
        r"\boutsystems\b",
        r"\blow[-\s]?code\b",
        r"\bwordpress\b",
        r"\bdrupal\b",
        r"\bmagento\b",
        r"\bsharepoint\b",
    ],

    # Reject when the BODY couples an on-site / hybrid signal with a city outside the
    # Wrocław area (the listing's location field frequently says "remote"/"Poland"
    # while the description demands N days/week in a Kraków/Warsaw/foreign office).
    "exclude_body_onsite_city": True,

    # Exception to the two location gates above: KEEP a hybrid role that only needs
    # the office ~1 day/week, but ONLY for Warsaw / Kraków (commutable from Wrocław
    # once a week). Detected from the body frequency phrasing. More than 1 day/week,
    # an unspecified frequency, or any other far city → still rejected.
    "allow_weekly_hybrid_warsaw_krakow": True,

    # Reject AI-data-labeling / staffing-mill roles by company name (titles are often
    # clean "Angular Developer" so only the company gives them away — micro1 fronts).
    "exclude_ai_training": True,
    "exclude_companies": [
        "micro1",
        "alignerr",
        "quikhire",
        "hirefeed",
        "mercor",
        "outlier ai",
    ],

    # Drop roles that require German (checked in title + location + raw description-like fields).
    # Set false if you speak German or use boards where this produces false positives.
    "exclude_german_language_required": True,

    # Drop part-time / very short contract roles (checked in full job text, not only title).
    # Catches cases where "part-time" appears in the description but not the job title.
    "exclude_unacceptable_contract": True,

    # Drop jobs that explicitly require relocation outside Poland / outside Wrocław region.
    # Catches "hybrid Helsinki", "relocation to Barcelona required", etc. in the full text.
    "exclude_relocation_required": True,

    # Extra anti-hybrid cities appended to _ANTI_HYBRID_CITIES in filters.py.
    # These are non-Polish cities that appeared as hybrid requirements in the tracker.
    "extra_anti_hybrid_cities": [
        # EU cities outside Poland that appeared in hybrid job descriptions
        "helsinki", "helsingfors",
        "barcelona", "madrid", "lisbon", "lisboa",
        "berlin", "munich", "münchen", "hamburg", "frankfurt",
        "amsterdam", "rotterdam",
        "prague", "brno",
        "bratislava",
        "budapest",
        "bucharest",
        "sofia",
        "zagreb",
        # Cyprus (recruiter posts / XM, GRS — hybrid in Limassol/Nicosia/Larnaca)
        "limassol", "nicosia", "larnaca", "larnaka", "paphos", "pafos",
        # Non-EU / remote-but-actually-not regions
        "islamabad", "karachi", "lahore",   # Pakistan
        "bangalore", "mumbai", "delhi",     # India
        "singapore",
        "dubai", "abu dhabi",
        "hong kong",
        "tokyo",
    ],
}
