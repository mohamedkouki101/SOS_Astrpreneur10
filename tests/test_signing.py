import copy

import pytest
from pydantic import ValidationError

from ingest.contract import IngestedDocument
from ingest.signing import canonical_json, sign_document, verify_envelope

KEY = b"k" * 64
OTHER_KEY = b"x" * 64


def signed(envelope: dict) -> dict:
    """Validate, sign, and return the JSON dict that would be sent."""
    doc = sign_document(IngestedDocument.model_validate(envelope), KEY)
    return doc.model_dump(mode="json")


def test_canonical_json_is_exact():
    assert canonical_json({"b": 1, "a": "é"}) == '{"a":"é","b":1}'.encode("utf-8")


def test_canonical_json_rejects_floats():
    with pytest.raises(ValueError):
        canonical_json({"scan": {"risk_score_bp": 0.62}})


def test_sign_then_verify(envelope):
    sent = signed(envelope)
    assert sent["signature"].startswith("hmac-sha256:")
    assert verify_envelope(sent, KEY)


def test_signed_document_still_passes_contract(envelope):
    IngestedDocument.model_validate(signed(envelope))


def test_signature_survives_json_round_trip(envelope):
    import json
    sent = json.loads(json.dumps(signed(envelope)))
    assert verify_envelope(sent, KEY)


def test_one_changed_character_fails(envelope):
    sent = signed(envelope)
    tampered = copy.deepcopy(sent)
    tampered["scan"]["verdict"] = "clean"
    assert not verify_envelope(tampered, KEY)


def test_wrong_key_fails(envelope):
    assert not verify_envelope(signed(envelope), OTHER_KEY)


@pytest.mark.parametrize("value", [None, 123, "", "hmac-sha256:" + "0" * 64, "hmac-sha256:é"])
def test_missing_or_bad_signature_fails(envelope, value):
    sent = signed(envelope)
    sent["signature"] = value
    assert not verify_envelope(sent, KEY)


# --- Review fixes ---

def test_document_changed_after_validation_cannot_be_signed(envelope):
    envelope["scan"]["verdict"] = "quarantined"
    envelope["chunks"] = []
    doc = IngestedDocument.model_validate(envelope)
    doc.scan.signals.append("NOT A VALID SIGNAL")  # lists can be changed in place
    with pytest.raises(ValidationError):
        sign_document(doc, KEY)


@pytest.mark.parametrize("hostile", [
    "not a dict",
    [1, 2],
    {"signature": "hmac-sha256:" + "0" * 64, "a": 1.5},
    {"signature": "hmac-sha256:" + "0" * 64, "a": (1.5,)},
])
def test_verify_fails_closed_on_hostile_input(hostile):
    assert verify_envelope(hostile, KEY) is False


def test_verify_fails_closed_on_deep_nesting():
    deep: list = []
    for _ in range(100_000):
        deep = [deep]
    assert verify_envelope({"signature": "x", "a": deep}, KEY) is False


@pytest.mark.parametrize("obj", [{"a": (1.5,)}, {1.0: "x"}])
def test_canonical_json_rejects_tuple_floats_and_non_string_keys(obj):
    with pytest.raises(ValueError):
        canonical_json(obj)


@pytest.mark.parametrize("key", [b"", b"short", "k" * 64])
def test_weak_or_wrong_type_key_is_refused(envelope, key):
    doc = IngestedDocument.model_validate(envelope)
    with pytest.raises(ValueError):
        sign_document(doc, key)
    with pytest.raises(ValueError):
        verify_envelope(envelope, key)
