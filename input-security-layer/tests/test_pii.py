"""PII masking on fictional CVs. Every name, number and address here is invented."""

import pytest

from ingest.pii.language import detect_language
from ingest.pii.mask import mask
from ingest.pii.refs import email_ref

FRENCH_CV = """Nom : Amina Ben Salem
Email : amina.bensalem@example.test
Tél : +216 22 123 456
CIN : 01234567
Âge : 27 ans
Sexe : F
Situation familiale : célibataire
Nationalité : tunisienne
Né le 12/03/1999
LinkedIn : hxxps://www[.]linkedin[.]com/in/amina-fictional

Expérience
Ingénieure données chez Fictional Corp, 3 ans d'expérience en Python et SQL.
Amina Ben Salem a dirigé une équipe de 4 personnes. Contact : amina.bensalem@example.test"""

ENGLISH_CV = """Name: Sarah Fictional
Phone: 22 987 654
Gender: Female
Marital status: married
Date of birth: 04/07/1995

Experience
Data analyst with 5 years of experience in the team. Sarah Fictional also worked with the support team."""


@pytest.fixture(scope="module")
def french():
    return mask(FRENCH_CV)


def test_no_original_value_left(french):
    for value in ("Amina", "Ben Salem", "amina.bensalem", "22 123 456", "01234567",
                  "27 ans", "célibataire", "tunisienne", "12/03/1999", "linkedin[.]com"):
        assert value.lower() not in french.masked_text.lower(), value


def test_same_value_same_token(french):
    by_token = {item.token: item for item in french.items}
    assert by_token["<PERSON_1>"].original == "Amina Ben Salem"
    assert by_token["<PERSON_1>"].count == 2
    assert by_token["<EMAIL_1>"].count == 2
    assert french.masked_text.startswith("Nom : <PERSON_1>\nEmail : <EMAIL_1>\nTél : <PHONE_1>\nCIN : <CIN_1>")


def test_protected_attributes_removed_with_label_kept(french):
    assert "Âge : [REMOVED]" in french.masked_text
    assert "Sexe : [REMOVED]" in french.masked_text
    assert "Nationalité : [REMOVED]" in french.masked_text
    assert french.protected_removed == ["AGE", "BIRTH_DATE", "GENDER", "MARITAL_STATUS", "NATIONALITY"]


def test_job_relevant_text_kept(french):
    assert "3 ans d'expérience en Python et SQL" in french.masked_text
    assert "Fictional Corp" in french.masked_text


def test_profile_and_language(french):
    assert "<PROFILE_URL_1>" in french.masked_text
    assert french.language == "fr"
    assert french.first_email == "amina.bensalem@example.test"
    assert set(french.mapping) == set(french.tokens)


def test_english_cv():
    result = mask(ENGLISH_CV)
    assert result.language == "en"
    assert "Sarah" not in result.masked_text
    assert "22 987 654" not in result.masked_text
    assert "Gender: [REMOVED]" in result.masked_text
    assert "5 years of experience" in result.masked_text
    assert result.protected_removed == ["BIRTH_DATE", "GENDER", "MARITAL_STATUS"]


def test_free_text_name_found_by_model():
    result = mask("Bonjour, je m'appelle Karim Trabelsi et je suis développeur à Sfax.")
    assert "Karim Trabelsi" not in result.masked_text
    assert "<PERSON_1>" in result.masked_text


def test_tokens_match_contract_format(french):
    import re
    assert all(re.fullmatch(r"<[A-Z_]+_[1-9][0-9]{0,3}>", token) for token in french.tokens)


# --- Review fixes ---

@pytest.mark.parametrize("text, leaked", [
    ("Email : amina.single@example.test", "single@"),          # protected word inside an email
    ("Date de naissance : 1999-03-12", "1999-03-12"),
    ("Date of birth: March 4, 1995", "1995"),
    ("Né en 1999 à Sfax", "1999"),
    ("carte d’identité : 01234567", "01234567"),
    ("CIN : 012 345 67", "012 345 67"),
    ("Contact : amina[@]example[.]test", "amina"),
])
def test_leaks_closed(text, leaked):
    assert leaked not in mask(text).masked_text


def test_age_never_partly_removed():
    # A 4-digit value is not an age: left whole, never cut into "[REMOVED]99".
    assert mask("Âge : 1999").masked_text == "Âge : 1999"
    assert mask("Âge : 27 ans").masked_text == "Âge : [REMOVED]"


@pytest.mark.parametrize("text", [
    "Built a single-page app with single sign-on",
    "Salaire souhaité : 30 000 000 DT",
    "Genre : Roman policier",
    "Candidate: Software Engineer\nSoftware Engineer with 5 years of experience",
])
def test_normal_text_left_alone(text):
    result = mask(text)
    assert result.masked_text == text
    assert result.removed == []


def test_empty_label_does_not_swallow_next_line():
    result = mask("Nom :\n\nExpérience\nExpérience en Python")
    assert result.masked_text == "Nom :\n\nExpérience\nExpérience en Python"


def test_obfuscated_email_is_canonical_for_candidate_ref():
    assert mask("amina [at] example [.] test").first_email == "amina@example.test"


def test_same_profile_same_token():
    result = mask("https://www.linkedin.com/in/amina and linkedin.com/in/amina")
    assert result.masked_text == "<PROFILE_URL_1> and <PROFILE_URL_1>"


@pytest.mark.parametrize("text, expected", [
    ("Adresse : 12 rue de la Liberté, 3000 Sfax", "Adresse : <ADDRESS_1>"),
    ("Address: 5 Fictional Street, Tunis", "Address: <ADDRESS_1>"),
    ("Adresse postale : Résidence Fictive, Bloc B, Tunis", "Adresse postale : <ADDRESS_1>"),
])
def test_labeled_address_is_masked(text, expected):
    result = mask(text)
    assert result.masked_text == expected
    assert result.items[0].original == text.split(":", 1)[1].strip()


def test_name_parts_alone_get_the_full_name_token():
    text = ("Nom : Amina Ben Salem\n"
            "Amina a dirigé l'équipe. Mme Salem parle trois langues. Bureau de Ben Arous.")
    result = mask(text)
    assert result.masked_text == (
        "Nom : <PERSON_1>\n"
        "<PERSON_1> a dirigé l'équipe. Mme <PERSON_1> parle trois langues. Bureau de Ben Arous.")
    assert result.items[0].original == "Amina Ben Salem" and result.items[0].count == 3


def test_name_part_shared_by_two_names_is_left_alone():
    text = "Nom : Amina Salem\nRéférence : Karim Salem, manager.\nSalem a validé."
    assert "Salem a validé" in mask(text).masked_text


def test_short_name_parts_are_left_alone():
    assert "Jo" in mask("Nom : Jo Fictional\nJo aime Python.").masked_text


def test_masking_keeps_line_breaks_around_names():
    result = mask("Nom : Karim Fictif\nCompétences : Java\nKarim Fictif a dirigé l'équipe.")
    assert result.masked_text == "Nom : <PERSON_1>\nCompétences : Java\n<PERSON_1> a dirigé l'équipe."
    assert result.items[0].original == "Karim Fictif"


def test_works_with_networking_blocked(monkeypatch):
    """The container has no internet: loading and masking must never try to connect."""
    import socket

    from ingest.pii import mask as mask_module

    def refuse(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    mask_module._analyzer.cache_clear()  # reload Presidio and spaCy under the block
    result = mask("Nom : Amina Ben Salem\nTél : +216 22 123 456\nhttps://example.test")
    assert result.masked_text.startswith("Nom : <PERSON_1>\nTél : <PHONE_1>")


def test_language_detection():
    assert detect_language("Expérience et compétences pour le poste") == "fr"
    assert detect_language("Experience and skills for the role") == "en"
    assert detect_language("12345") == "unknown"


def test_email_ref_normalizes_and_hides_email():
    key = b"r" * 32
    ref = email_ref("  Amina@Example.TEST ", key)
    assert ref == email_ref("amina@example.test", key)
    assert ref.startswith("hmac:") and "amina" not in ref
    assert ref != email_ref("amina@example.test", b"s" * 32)


@pytest.mark.parametrize("text", [
    "Bonjour, combien de jours de Privilege Leave il me reste ?",
    "How many days of Sick Leave do I have left?",
    "Je voudrais poser un Congé Annuel la semaine prochaine.",
])
def test_hr_terms_are_not_masked_as_names(text):
    assert "<PERSON_" not in mask(text).masked_text


def test_name_next_to_hr_terms_still_masked():
    text = "Bonjour, je suis Amina Ben Salem et je voudrais poser un Congé Annuel."
    assert "Amina" not in mask(text).masked_text
