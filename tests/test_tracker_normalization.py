import pytest
from hunter.tracker import dedup_key, normalize_company, normalize_url


# ---------------------------------------------------------------------------
# normalize_url
# ---------------------------------------------------------------------------

def test_normalize_url_removes_tracking_query_params() -> None:
    url = (
        "https://www.pracuj.pl/praca/senior-frontend,wroclaw,oferta,1004225555"
        "?sendid=123&utm_source=newsletter&sug=abc"
    )
    normalized = normalize_url(url)
    assert normalized == "https://www.pracuj.pl/praca/senior-frontend,wroclaw,oferta,1004225555"


def test_normalize_url_keeps_non_tracking_query_params() -> None:
    url = "https://example.com/jobs/view/42?lang=en&page=2&utm_source=ad"
    normalized = normalize_url(url)
    assert normalized == "https://example.com/jobs/view/42?lang=en&page=2"


def test_normalize_url_linkedin_view_strips_extra_parts() -> None:
    url = "https://www.linkedin.com/jobs/view/1234567890/?trackingId=abc"
    normalized = normalize_url(url)
    assert normalized == "https://www.linkedin.com/jobs/view/1234567890"


# ---------------------------------------------------------------------------
# normalize_company — legal suffix stripping
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("variant", [
    "Mindbox Sp. z o.o.",   # properly formatted
    "Mindbox Sp z o o",     # no dots
    "Mindbox sp.z.o.o.",    # no spaces
    "MindboxSpZOo",         # squished CamelCase (LLM folder form)
    "MindboxSpZoo",         # lowercase 'oo'
    "Mindbox spzoo",        # all-lower squished
    "MINDBOX",              # uppercase only
    "mindbox",              # plain
])
def test_normalize_company_mindbox_variants(variant: str) -> None:
    assert normalize_company(variant) == "mindbox", f"Failed for: {variant!r}"


@pytest.mark.parametrize("variant", [
    "Upvanta Sp. z o.o.",
    "Upvanta Spółka z o.o.",
    "UpvantaSpółkaZOgraniczonąOdpowiedzialnoś",   # real tracker example
    "Upvanta Spolka z o.o.",
    "Upvanta",
])
def test_normalize_company_upvanta_variants(variant: str) -> None:
    assert normalize_company(variant) == "upvanta", f"Failed for: {variant!r}"


@pytest.mark.parametrize("variant", [
    "Acme Sp. z o.o.",
    "ACME",
    "Acme S.A.",
    "Acme Ltd.",
    "Acme GmbH",
    "Acme Inc.",
])
def test_normalize_company_acme_variants(variant: str) -> None:
    assert normalize_company(variant) == "acme", f"Failed for: {variant!r}"


# ---------------------------------------------------------------------------
# dedup_key — company+title dedup stable across name variants
# ---------------------------------------------------------------------------

def test_dedup_key_is_stable_for_company_variations() -> None:
    k1 = dedup_key("Acme Sp. z o.o.", "Senior Frontend Developer")
    k2 = dedup_key("ACME", "Senior Frontend Developer")
    assert k1 == k2


def test_dedup_key_squished_vs_formatted() -> None:
    k1 = dedup_key("MindboxSpZOo", "Angular Developer")
    k2 = dedup_key("Mindbox Sp. z o.o.", "Angular Developer")
    assert k1 == k2


def test_dedup_key_polish_form_vs_short() -> None:
    k1 = dedup_key("UpvantaSpółkaZOgraniczonąOdpowiedzialnoś", "Frontend Engineer")
    k2 = dedup_key("Upvanta", "Frontend Engineer")
    assert k1 == k2
