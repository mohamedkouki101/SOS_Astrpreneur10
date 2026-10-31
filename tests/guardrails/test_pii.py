"""Guardrail: PII is masked and protected attributes are removed before scoring or any model call."""

from agents.ingest import pipeline
from agents.ingest.pii import TokenStore, mask
from tests.conftest import DATASET, needs_dataset


def test_known_identifiers_and_protected_lines():
    text = ("AMIRA TRABELSI\namira.trabelsi@example.com | +216 22 345 678 | Tunis\n"
            "Date of birth: 1990-01-01   Nationality: Tunisian   Marital status: Married\nGender: Female\n"
            "Skills: Python, SQL, Docker. Amira led the data team.")
    res = mask(text, TokenStore(), {"PERSON": "Amira Trabelsi", "EMAIL": "amira.trabelsi@example.com",
                                     "PHONE": "+216 22 345 678"})
    for leaked in ("Amira", "Trabelsi", "amira.trabelsi@example.com", "345 678", "1990", "Tunisian", "Female", "Married"):
        assert leaked.lower() not in res.text.lower(), leaked
    assert "[PERSON_1]" in res.text and "[EMAIL_1]" in res.text
    assert "Python" in res.text and "Docker" in res.text          # job-related content is kept
    assert res.removed_protected_lines == 2


def test_tokens_are_stable():
    store = TokenStore()
    a = mask("contact x@example.com", store).text
    b = mask("again x@example.com", store).text
    assert a.split()[-1] == b.split()[-1]


def test_emails_on_internal_or_reserved_domains_are_masked():
    """Presidio validates TLDs; internal domains like .example / .local must still be masked."""
    res = mask("From: rania.abidi@bara3am.example\nCc: hr@rhflow.local, x.y+1@sub.corp.internal", TokenStore())
    for leaked in ("rania.abidi", "bara3am.example", "hr@rhflow.local", "corp.internal"):
        assert leaked not in res.text, leaked
    assert res.text.count("[EMAIL_") == 3


@needs_dataset
def test_every_dataset_email_header_is_masked():
    import re
    from email import message_from_bytes

    for path in sorted((DATASET / "emails").glob("*.eml")):
        res = pipeline.ingest_bytes(f"email/{path.stem}", path.name, path.read_bytes())
        if res.status != "ok":
            continue
        for header in ("From", "To", "Cc"):
            assert not re.search(r"[\w.+-]+@[\w-]+\.", res.headers.get(header, "")), (path.name, header)
        sender = message_from_bytes(path.read_bytes())["From"]
        assert sender not in res.text, path.name


@needs_dataset
def test_dataset_cv_has_no_contact_data_after_pipeline():
    import re

    for name in ("APP-PY-02.pdf", "APP-SR-07.pdf"):
        res = pipeline.ingest_bytes(f"cv/{name}", name, (DATASET / "cvs" / name).read_bytes())
        assert res.status == "ok"
        assert not re.search(r"[\w.]+@[\w.]+\.\w+", res.text)
        assert "date of birth" not in res.text.lower()
