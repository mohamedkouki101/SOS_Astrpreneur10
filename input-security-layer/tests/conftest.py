import pytest

from ingest.contract import sha256_id

# Fictional data only.
CHUNK_TEXT = "<PERSON_1> worked 3 years as a data engineer at <ORG_1>."


@pytest.fixture
def envelope() -> dict:
    """A valid v1.1 envelope as plain JSON data (what the AI layer receives)."""
    return {
        "schema_version": "1.1",
        "doc_id": "sha256:" + "a" * 64,
        "candidate_ref": "hmac:" + "b" * 64,
        "source": {
            "type": "upload_cv",
            "message_id": None,
            "in_reply_to": None,
            "file_ext": "pdf",
            "received_at": "2026-10-31T18:40:00Z",
            "sender_ref": None,
        },
        "job_opening": {"id": "HR-OPN-0004", "origin": "recruiter_upload"},
        "language": "fr",
        "trust": "untrusted",
        "scan": {
            "verdict": "suspicious",
            "risk_score_bp": 6200,
            "signals": ["hidden_text_removed:2", "rule:ignore_previous_fr"],
        },
        "pii": {
            "masked": True,
            "token_scope": "doc",
            "tokens": ["<PERSON_1>", "<ORG_1>"],
            "protected_removed": ["AGE", "GENDER"],
        },
        "chunks": [{"chunk_id": sha256_id(CHUNK_TEXT.encode("utf-8")), "section": "experience", "text": CHUNK_TEXT}],
        "pipeline_version": "ingest-1.1",
        "signature": None,
    }


@pytest.fixture
def email_source() -> dict:
    """A valid email_body source, to swap into the envelope."""
    return {
        "type": "email_body",
        "message_id": "<demo-001@mail.example.test>",
        "in_reply_to": None,
        "file_ext": None,
        "received_at": "2026-10-31T18:40:00Z",
        "sender_ref": "hmac:" + "c" * 64,
    }
