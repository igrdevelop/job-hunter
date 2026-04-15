from hunter.tracker import dedup_key, normalize_url


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


def test_dedup_key_is_stable_for_company_variations() -> None:
    k1 = dedup_key("Acme Sp. z o.o.", "Senior Frontend Developer")
    k2 = dedup_key("ACME", "Senior Frontend Developer")
    assert k1 == k2
