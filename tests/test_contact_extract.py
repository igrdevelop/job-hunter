"""hunter/contact_extract.py — deterministic recruiter-contact extraction (#138)."""

from hunter.contact_extract import Contact, extract_contacts


# ── Labeled names (PL + EN) ──────────────────────────────────────────────────


def test_labeled_kontakt_polish() -> None:
    text = "Opis stanowiska...\nKontakt: Anna Kowalska\nAplikuj przez formularz."
    contacts = extract_contacts(text)
    assert contacts[0].name == "Anna Kowalska"
    assert "Kontakt" in contacts[0].evidence


def test_labeled_recruiter_english() -> None:
    contacts = extract_contacts("Recruiter: John Smith\nApply below.")
    assert contacts[0].name == "John Smith"


def test_labeled_osoba_kontaktowa_with_diacritics() -> None:
    contacts = extract_contacts("Osoba kontaktowa: Łukasz Zieliński")
    assert contacts[0].name == "Łukasz Zieliński"


def test_labeled_aplikuj_do() -> None:
    contacts = extract_contacts("CV prześlij do: Maria Nowak do końca miesiąca")
    assert contacts[0].name == "Maria Nowak"


def test_label_followed_by_non_name_is_ignored() -> None:
    # "aplikuj do końca miesiąca" — lowercase words must not become a "name"
    assert extract_contacts("Aplikuj do końca miesiąca przez formularz.") == []


# ── Signature blocks ──────────────────────────────────────────────────────────


def test_signature_name_over_recruiter_role_line() -> None:
    text = "Wyślij CV!\n\nAnna Wiśniewska\nSenior IT Recruiter\nAntal Sp. z o.o."
    contacts = extract_contacts(text)
    assert contacts[0].name == "Anna Wiśniewska"


def test_signature_polish_role_line() -> None:
    text = "Piotr Zając\nSpecjalista ds. rekrutacji IT"
    contacts = extract_contacts(text)
    assert contacts[0].name == "Piotr Zając"


def test_name_over_unrelated_line_is_not_a_signature() -> None:
    text = "Angular Developer\nWarszawa, Polska"
    assert all(not c.name for c in extract_contacts(text))


# ── Emails ────────────────────────────────────────────────────────────────────


def test_bare_recruiting_email_becomes_contact() -> None:
    contacts = extract_contacts("Wyślij CV na rekrutacja@agencja.pl do 30.06.")
    assert contacts[0].email == "rekrutacja@agencja.pl"
    assert not contacts[0].name


def test_noreply_and_rodo_emails_skipped() -> None:
    text = "noreply@portal.pl oraz rodo@firma.pl — administratorem danych..."
    assert extract_contacts(text) == []


def test_email_attaches_to_matching_name() -> None:
    text = "Kontakt: Anna Kowalska\nEmail: anna.kowalska@antal.pl"
    contacts = extract_contacts(text)
    assert contacts[0].name == "Anna Kowalska"
    assert contacts[0].email == "anna.kowalska@antal.pl"
    assert len(contacts) == 1  # not duplicated as a separate email-only entry


def test_email_folds_diacritics_when_matching_name() -> None:
    text = "Rekruter: Łukasz Zieliński\nlukasz.zielinski@firma.pl"
    contacts = extract_contacts(text)
    assert contacts[0].email == "lukasz.zielinski@firma.pl"


def test_unrelated_email_stays_separate_contact() -> None:
    text = "Kontakt: Anna Kowalska\njobs@firma.pl"
    contacts = extract_contacts(text)
    assert contacts[0].name == "Anna Kowalska" and not contacts[0].email
    assert contacts[1].email == "jobs@firma.pl"


# ── Phones ────────────────────────────────────────────────────────────────────


def test_plus48_phone_attaches_to_first_contact() -> None:
    text = "Kontakt: Anna Kowalska, tel. +48 601 234 567"
    contacts = extract_contacts(text)
    assert contacts[0].phone.startswith("+48")


def test_salary_range_is_not_a_phone() -> None:
    text = "Kontakt: Anna Kowalska\nWynagrodzenie: 18 000 - 24 000 PLN brutto"
    contacts = extract_contacts(text)
    assert contacts[0].phone == ""


def test_bare_phone_without_contact_is_not_a_contact() -> None:
    assert extract_contacts("Zadzwoń: +48 601 234 567") == []


# ── General ───────────────────────────────────────────────────────────────────


def test_empty_text() -> None:
    assert extract_contacts("") == []


def test_no_contact_in_typical_corporate_posting() -> None:
    text = (
        "Senior Angular Developer\nWe are looking for an experienced developer "
        "to join our team. Requirements: Angular 17, RxJS, NgRx. We offer "
        "remote work and great benefits. Apply via the button below."
    )
    assert extract_contacts(text) == []


def test_cap_at_three_contacts() -> None:
    text = "\n".join(f"kandydat{i}@agencja.pl piszcie" for i in range(6))
    assert len(extract_contacts(text)) == 3


def test_duplicate_name_reported_once() -> None:
    text = "Kontakt: Anna Kowalska\n...\nRecruiter: Anna Kowalska"
    contacts = extract_contacts(text)
    assert [c.name for c in contacts] == ["Anna Kowalska"]


def test_contact_dataclass_defaults() -> None:
    c = Contact()
    assert (c.name, c.email, c.phone, c.evidence) == ("", "", "", "")
