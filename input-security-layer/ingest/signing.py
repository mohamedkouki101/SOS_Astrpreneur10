"""Envelope signing (contract v1.1).

signature = HMAC-SHA256(INGEST_SIGNING_KEY, canonical JSON of the envelope without "signature").
The AI layer uses the same canonical_json function, byte for byte.

Keys are passed in as bytes (not read from settings here), so the functions
are pure and easy to test. Callers use:
    settings.ingest_signing_key.get_secret_value().encode("utf-8")
"""

import hashlib
import hmac
import json
from typing import Any

from ingest.config import MIN_KEY_LENGTH
from ingest.contract import IngestedDocument

SIGNATURE_PREFIX = "hmac-sha256:"


def _check_key(key: bytes) -> None:
    # Same minimum as config.py, so an empty or short key can never sign anything.
    if not isinstance(key, bytes) or len(key) < MIN_KEY_LENGTH:
        raise ValueError(f"signing key must be at least {MIN_KEY_LENGTH} bytes")


def _reject_floats(value: Any) -> None:
    # Floats can be printed in more than one way, which would break signatures.
    # The models already forbid them; this is a second line of defense.
    if isinstance(value, float):
        raise ValueError("floats are not allowed in the envelope")
    if isinstance(value, dict):
        for name, item in value.items():
            if not isinstance(name, str):
                raise ValueError("envelope keys must be strings")
            _reject_floats(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_floats(item)


def canonical_json(obj: Any) -> bytes:
    """The exact serialization from the contract. Do not change without the AI layer."""
    _reject_floats(obj)
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_signature(envelope: dict, key: bytes) -> str:
    """Sign a plain JSON dict, ignoring any existing "signature" field."""
    _check_key(key)
    unsigned = {name: value for name, value in envelope.items() if name != "signature"}
    digest = hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def sign_document(doc: IngestedDocument, key: bytes) -> IngestedDocument:
    """Validate the document again, then return a signed copy.

    Validating again here means a document changed after it was first built
    (e.g. a chunk appended to a list) can never receive a valid signature.
    """
    envelope = doc.model_dump(mode="json")
    IngestedDocument.model_validate(envelope)  # raises if anything is wrong
    envelope["signature"] = compute_signature(envelope, key)
    return IngestedDocument.model_validate(envelope)  # fresh copy, shares no lists


def verify_envelope(envelope: Any, key: bytes) -> bool:
    """Check a received JSON envelope. Works on the raw dict, like the AI side does.

    Fails closed: any malformed input returns False instead of raising.
    A bad key is a configuration error, so that one still raises.
    """
    _check_key(key)
    if not isinstance(envelope, dict):
        return False
    received = envelope.get("signature")
    if not isinstance(received, str):
        return False
    try:
        expected = compute_signature(envelope, key)
    except (ValueError, TypeError, RecursionError):
        return False
    # Constant-time comparison: timing does not reveal how many characters matched.
    return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))
