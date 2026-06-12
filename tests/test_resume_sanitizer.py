"""Tests for hunter/resume_sanitizer.py"""

from unittest.mock import patch

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

REAL_ROLES = [
    {
        "company": "Alten Poland",
        "period": "Apr 2026 - May 2026",
        "subtitle": "Intel (client) | OpenVINO LLM Evaluation Tooling | Wrocław, Poland",
        "title": "Frontend Developer (Angular, part-time contract)",
        "parsed": (202604, 202605),
        "idx": 0,
    },
    {
        "company": "Fairmarkit (via contractor)",
        "period": "Jun 2025 - March 2026",
        "subtitle": "AI-powered Enterprise Procurement Platform | USA (Global)",
        "title": "Senior Frontend Developer (Angular)",
        "parsed": (202506, 202603),
        "idx": 1,
    },
    {
        "company": "Venture Labs",
        "period": "July 2023 - April 2025",
        "subtitle": "Banking Sector | Carbon Footprint Calculations | Poland | Client: Atruvia AG",
        "title": "Senior Frontend Developer (Angular)",
        "parsed": (202307, 202504),
        "idx": 2,
    },
    {
        "company": "SII",
        "period": "November 2022 - July 2023",
        "subtitle": "Finance Sector | Financial Instruments Management",
        "title": "Senior Frontend Developer (Angular)",
        "parsed": (202211, 202307),
        "idx": 3,
    },
    {
        "company": "Altoros",
        "period": "April 2018 - November 2022",
        "subtitle": "E-commerce | Insurance | Healthcare | Grant Management",
        "title": "Senior Frontend Developer (Angular)",
        "parsed": (201804, 202211),
        "idx": 4,
    },
    {
        "company": "SolbegSoft",
        "period": "April 2016 - April 2018",
        "subtitle": "Maintenance Services Management",
        "title": "Frontend Developer (Angular)",
        "parsed": (201604, 201804),
        "idx": 5,
    },
    {
        "company": "Staronka",
        "period": "November 2015 - March 2016",
        "subtitle": "Startup | Website Builder",
        "title": "Frontend Developer",
        "parsed": (201511, 201603),
        "idx": 6,
    },
]

REAL_EDUCATION = "Belarusian State Technological University - Bachelor, PE and Systems of Information Processing"
REAL_COURSES = (
    "Angular Updates Course, Angular Advanced Course, Angular Core Course, "
    "JS Architecture Workshop, RxJS Course, Java basic Course, Node.js Course, "
    "JavaScript Advanced Level"
)


def _patch_profile(roles=None, edu=None, courses=None):
    """Helper: patch the two profile loaders."""
    return (
        patch("hunter.resume_sanitizer._load_profile_roles", return_value=roles or REAL_ROLES),
        patch(
            "hunter.resume_sanitizer._load_profile_education_courses",
            return_value=(
                edu if edu is not None else REAL_EDUCATION,
                courses if courses is not None else REAL_COURSES,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# _parse_period_date
# ---------------------------------------------------------------------------

def test_parse_period_date_month_year():
    from hunter.resume_sanitizer import _parse_period_date
    assert _parse_period_date("Apr 2026") == 202604
    assert _parse_period_date("November 2022") == 202211
    assert _parse_period_date("June 2025") == 202506


def test_parse_period_date_bare_year():
    from hunter.resume_sanitizer import _parse_period_date
    assert _parse_period_date("2018") == 201801


def test_parse_period_date_unparseable():
    from hunter.resume_sanitizer import _parse_period_date
    assert _parse_period_date("present") is None
    assert _parse_period_date("") is None


# ---------------------------------------------------------------------------
# _parse_period
# ---------------------------------------------------------------------------

def test_parse_period_normal():
    from hunter.resume_sanitizer import _parse_period
    assert _parse_period("Apr 2026 - May 2026") == (202604, 202605)
    assert _parse_period("November 2022 - July 2023") == (202211, 202307)


def test_parse_period_present():
    from hunter.resume_sanitizer import _parse_period
    start, end = _parse_period("Jun 2025 - present")
    assert start == 202506
    assert end == 209912


def test_parse_period_bare_years():
    from hunter.resume_sanitizer import _parse_period
    assert _parse_period("2014 - 2017") == (201401, 201712)


# ---------------------------------------------------------------------------
# _is_real_company
# ---------------------------------------------------------------------------

def test_is_real_company_exact():
    with _patch_profile()[0]:
        # reload whitelist with patched data
        import hunter.resume_sanitizer as s
        s._load_profile_roles.cache_clear()
        with _patch_profile()[0]:
            assert s._is_real_company("Fairmarkit (via contractor)")
            assert s._is_real_company("SII")
            assert s._is_real_company("Alten Poland")


def test_is_real_company_fake():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        assert not s._is_real_company("Experis Poland (ManpowerGroup)")
        assert not s._is_real_company("LiveChat (Text, Inc.)")
        assert not s._is_real_company("Avenga (formerly IT Labs)")
        assert not s._is_real_company("Freelance & Contract Work")


# ---------------------------------------------------------------------------
# sanitize_resume — education / courses
# ---------------------------------------------------------------------------

def test_sanitize_fills_missing_education():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {"experience": [], "education": "", "courses": REAL_COURSES}
        result, fixes = s.sanitize_resume(resume, lang="EN")
        assert result["education"] == REAL_EDUCATION
        assert any("education filled" in f for f in fixes)


def test_sanitize_fills_missing_courses():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {"experience": [], "education": REAL_EDUCATION, "courses": None}
        result, fixes = s.sanitize_resume(resume, lang="EN")
        assert result["courses"] == REAL_COURSES
        assert any("courses filled" in f for f in fixes)


def test_sanitize_leaves_real_education_untouched():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {"experience": [], "education": REAL_EDUCATION, "courses": REAL_COURSES}
        result, fixes = s.sanitize_resume(resume, lang="EN")
        assert not fixes


# ---------------------------------------------------------------------------
# sanitize_resume — company replacement
# ---------------------------------------------------------------------------

def _make_fake_entry(company, period, bullets=None, stack_line="Stack: Angular."):
    return {
        "title": "Senior Developer",
        "company": company,
        "period": period,
        "subtitle": "Some Industry",
        "bullets": bullets or ["did something"],
        "stack_line": stack_line,
    }


def test_sanitize_replaces_experis_with_sii():
    """Experis Poland Feb 2022 - Jun 2023 overlaps with SII Nov 2022 - Jul 2023."""
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        original_bullets = ["Built 3D viz with Three.js", "Optimized performance"]
        resume = {
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "experience": [_make_fake_entry("Experis Poland (ManpowerGroup)", "Feb 2022 - June 2023",
                                            bullets=original_bullets)],
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        entry = result["experience"][0]
        assert entry["company"] == "SII"
        assert entry["period"] == "November 2022 - July 2023"
        assert entry["bullets"] == original_bullets  # bullets preserved
        assert "Experis Poland" in fixes[0]
        assert "SII" in fixes[0]


def test_sanitize_replaces_avenga_with_altoros():
    """Avenga Sep 2019 - Jan 2022 overlaps with Altoros Apr 2018 - Nov 2022."""
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "experience": [_make_fake_entry("Avenga (formerly IT Labs)", "Sep 2019 - Jan 2022")],
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        assert result["experience"][0]["company"] == "Altoros"


def test_sanitize_replaces_livechat_with_solbegsoft():
    """LiveChat Dec 2017 - Aug 2019 overlaps with SolbegSoft Apr 2016 - Apr 2018 (and Altoros).
    With Altoros already consumed in a previous entry, SolbegSoft should be next best."""
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "experience": [
                _make_fake_entry("Avenga (formerly IT Labs)", "Sep 2019 - Jan 2022"),
                _make_fake_entry("LiveChat (Text, Inc.)", "Dec 2017 - Aug 2019"),
            ],
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        companies = [e["company"] for e in result["experience"]]
        assert "Altoros" in companies
        assert "SolbegSoft" in companies


def test_sanitize_leaves_real_companies_untouched():
    """Real companies must not be replaced; titles are corrected to match profile."""
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "experience": [
                _make_fake_entry("Fairmarkit", "Jun 2025 - March 2026"),
                _make_fake_entry("Venture Labs", "July 2023 - April 2025"),
                _make_fake_entry("SII", "November 2022 - July 2023"),
            ],
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        companies = [e["company"] for e in result["experience"]]
        # Companies must be preserved
        assert "Fairmarkit" in companies
        assert "Venture Labs" in companies
        assert "SII" in companies
        # No company replacements should occur — only title fixes are allowed
        company_replacement_fixes = [f for f in fixes if "→" in f and "title" not in f]
        assert not company_replacement_fixes


def test_sanitize_real_case_emagine():
    """Simulate the EmaginePolska hallucination: 4 fake companies in EN resume."""
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume_en = {
            "education": "",   # missing in real case
            "courses": None,   # missing in real case
            "experience": [
                _make_fake_entry("Alten Poland", "Apr 2026 - May 2026"),
                _make_fake_entry("Fairmarkit (via contractor)", "Jun 2025 - March 2026"),
                _make_fake_entry("Venture Labs", "July 2023 - April 2025"),
                _make_fake_entry("Experis Poland (ManpowerGroup)", "Feb 2022 - June 2023"),
                _make_fake_entry("Avenga (formerly IT Labs)", "Sep 2019 - Jan 2022"),
                _make_fake_entry("LiveChat (Text, Inc.)", "Dec 2017 - Aug 2019"),
                _make_fake_entry("Freelance & Contract Work", "2014 - 2017"),
            ],
        }
        result, fixes = s.sanitize_resume(resume_en, lang="EN")

        companies = [e["company"] for e in result["experience"]]
        # Real ones preserved
        assert "Alten Poland" in companies
        assert "Fairmarkit (via contractor)" in companies
        assert "Venture Labs" in companies
        # Fake ones replaced
        assert "Experis Poland (ManpowerGroup)" not in companies
        assert "Avenga (formerly IT Labs)" not in companies
        assert "LiveChat (Text, Inc.)" not in companies
        assert "Freelance & Contract Work" not in companies
        # Education / courses filled
        assert result["education"] == REAL_EDUCATION
        assert result["courses"] == REAL_COURSES
        # 4 company fixes + 2 education/courses fills + 1 courses coerce (None→str)
        # + 3 title fixes for real companies (Alten Poland, Fairmarkit, Venture Labs)
        assert len(fixes) == 10


def test_sanitize_collapses_duplicate_angular_versions():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {
            "experience": [],
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "skills": {"frontend": "Angular (latest versions), Angular 2-22, Angular Material, TypeScript, RxJS"},
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        fe = result["skills"]["frontend"]
        # Exactly one bare Angular version entry remains, canonical form
        assert "Angular (2-22)" in fe
        assert "Angular 2-22," not in fe and "latest versions" not in fe
        # Family skill preserved
        assert "Angular Material" in fe
        assert any("collapsed" in f and "Angular" in f for f in fixes)


def test_sanitize_keeps_single_angular_and_material():
    import hunter.resume_sanitizer as s
    s._load_profile_roles.cache_clear()
    s._load_profile_education_courses.cache_clear()
    p1, p2 = _patch_profile()
    with p1, p2:
        resume = {
            "experience": [],
            "education": REAL_EDUCATION,
            "courses": REAL_COURSES,
            "skills": {"frontend": "Angular (2-22), Angular Material, TypeScript"},
        }
        result, fixes = s.sanitize_resume(resume, lang="EN")
        assert result["skills"]["frontend"] == "Angular (2-22), Angular Material, TypeScript"
        assert not any("collapsed" in f for f in fixes)
