import pytest
from pydantic import ValidationError

from ingest.contract import IngestedDocument, sha256_id


def check(envelope: dict) -> IngestedDocument:
    return IngestedDocument.model_validate(envelope)


def test_valid_envelope_passes(envelope):
    doc = check(envelope)
    assert doc.trust == "untrusted"


def test_valid_email_envelope_passes(envelope, email_source):
    envelope["source"] = email_source
    envelope["job_opening"] = {"id": "HR-OPN-0004", "origin": "email_hint"}
    check(envelope)


def test_document_is_frozen(envelope):
    doc = check(envelope)
    with pytest.raises(ValidationError):
        doc.trust = "trusted"


# --- Types and formats ---

@pytest.mark.parametrize("value", [6200.0, "6200", True, -1, 10_001])
def test_risk_score_must_be_int_in_range(envelope, value):
    envelope["scan"]["risk_score_bp"] = value
    with pytest.raises(ValidationError):
        check(envelope)


@pytest.mark.parametrize("value", [
    "a" * 64,                      # missing prefix
    "sha256:" + "A" * 64,          # uppercase hex
    "sha256:" + "a" * 63,          # too short
    "sha256:" + "a" * 64 + "\n",   # trailing newline
])
def test_bad_doc_id_fails(envelope, value):
    envelope["doc_id"] = value
    with pytest.raises(ValidationError):
        check(envelope)


@pytest.mark.parametrize("token", ["PERSON_1", "<person_1>", "<PERSON_0>", "<PERSON_1><script>"])
def test_bad_mask_token_fails(envelope, token):
    envelope["pii"]["tokens"] = [token]
    with pytest.raises(ValidationError):
        check(envelope)


def test_trust_cannot_be_changed(envelope):
    envelope["trust"] = "trusted"
    with pytest.raises(ValidationError):
        check(envelope)


def test_unknown_field_fails(envelope):
    envelope["original_filename"] = "cv_fictional_name.pdf"
    with pytest.raises(ValidationError):
        check(envelope)


def test_impossible_date_fails(envelope):
    envelope["source"]["received_at"] = "2026-13-45T99:00:00Z"
    with pytest.raises(ValidationError):
        check(envelope)


def test_unknown_protected_attribute_fails(envelope):
    envelope["pii"]["protected_removed"] = ["SALARY"]
    with pytest.raises(ValidationError):
        check(envelope)


# --- Cross-field rules ---

def test_chunk_id_must_match_text(envelope):
    envelope["chunks"][0]["text"] += " (edited)"
    with pytest.raises(ValidationError):
        check(envelope)


def test_chunk_id_matches_utf8_text(envelope):
    text = "Expérience : ingénieure données, Sfax."
    envelope["chunks"] = [{"chunk_id": sha256_id(text.encode("utf-8")), "section": "experience", "text": text}]
    check(envelope)


def test_duplicate_chunk_fails(envelope):
    envelope["chunks"].append(dict(envelope["chunks"][0]))
    with pytest.raises(ValidationError):
        check(envelope)


def test_unmasked_document_with_chunks_fails(envelope):
    envelope["pii"]["masked"] = False
    with pytest.raises(ValidationError):
        check(envelope)


@pytest.mark.parametrize("field, value", [
    ("tokens", ["<PERSON_1>", "<PERSON_1>"]),
    ("protected_removed", ["AGE", "AGE"]),
])
def test_duplicate_pii_entries_fail(envelope, field, value):
    envelope["pii"][field] = value
    with pytest.raises(ValidationError):
        check(envelope)


def test_quarantined_document_has_no_chunks(envelope):
    envelope["scan"]["verdict"] = "quarantined"
    with pytest.raises(ValidationError):
        check(envelope)
    envelope["chunks"] = []
    check(envelope)


def test_upload_cannot_have_sender_ref(envelope):
    envelope["source"]["sender_ref"] = "hmac:" + "c" * 64
    with pytest.raises(ValidationError):
        check(envelope)


def test_email_needs_sender_ref(envelope, email_source):
    email_source["sender_ref"] = None
    envelope["source"] = email_source
    envelope["job_opening"] = None
    with pytest.raises(ValidationError):
        check(envelope)


def test_email_body_has_no_file_ext(envelope, email_source):
    email_source["file_ext"] = "pdf"
    envelope["source"] = email_source
    envelope["job_opening"] = None
    with pytest.raises(ValidationError):
        check(envelope)


def test_email_cannot_claim_trusted_job_origin(envelope, email_source):
    envelope["source"] = email_source  # job_opening.origin is still "recruiter_upload"
    with pytest.raises(ValidationError):
        check(envelope)
