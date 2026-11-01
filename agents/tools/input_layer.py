"""Read side of the input security layer for the agents (`input-security-layer/`, contract v1.1).

The agents only ever see what the layer released: masked, chunked, signed envelopes. They hold the
ENVELOPE key only, so they cannot upload, read inspection reports (real data) or resolve tokens.

Every envelope is checked before use:
- HMAC-SHA256 signature over the canonical JSON (same function as ingest/signing.py, byte for byte);
- trust == "untrusted", not quarantined, every chunk_id == sha256(chunk text).
A failed check is an alert, and the document is not used.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re

import httpx

from agents.config import get_settings
from agents.context.run_context import Source
from agents.orchestrator import audit

REPORT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
SIGNATURE_PREFIX = "hmac-sha256:"


class EnvelopeError(Exception):
    pass


def enabled() -> bool:
    return get_settings().input_layer == "service"


def canonical_json(obj) -> bytes:
    """Must match ingest.signing.canonical_json exactly (the envelope never contains floats)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify(envelope: dict, key: bytes) -> bool:
    if not isinstance(envelope, dict) or not isinstance(envelope.get("signature"), str):
        return False
    unsigned = {k: v for k, v in envelope.items() if k != "signature"}
    expected = SIGNATURE_PREFIX + hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()
    return hmac.compare_digest(envelope["signature"].encode(), expected.encode())


def check(envelope: dict, key: bytes) -> None:
    if not verify(envelope, key):
        raise EnvelopeError("invalid signature")
    if envelope.get("trust") != "untrusted":
        raise EnvelopeError("trust must be 'untrusted'")
    if envelope.get("scan", {}).get("verdict") == "quarantined":
        raise EnvelopeError("quarantined documents are never used")
    for chunk in envelope.get("chunks", []):
        if chunk["chunk_id"] != "sha256:" + hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest():
            raise EnvelopeError("chunk_id does not match its text")


def fetch(report_id: str) -> dict | None:
    """A released envelope, verified. None if it is not released (quarantined, rejected, unknown)."""
    s = get_settings()
    if not (s.ingest_envelope_key and s.ingest_signing_key):
        raise EnvelopeError("INGEST_ENVELOPE_API_KEY / INGEST_SIGNING_KEY are not configured")
    if not REPORT_ID.fullmatch(report_id or ""):
        raise EnvelopeError("invalid report id")
    r = httpx.get(f"{s.ingest_url}/v1/envelopes/{report_id}", headers={"X-API-Key": s.ingest_envelope_key}, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    envelope = r.json()
    try:
        check(envelope, s.ingest_signing_key.encode("utf-8"))
    except EnvelopeError as exc:
        audit.alert("envelope_rejected", report_id=report_id, reason=str(exc))
        raise
    return envelope


def text_of(envelope: dict) -> str:
    """Chunks as the model reads them; CV sections are labelled so the model knows the structure."""
    parts = []
    for chunk in envelope.get("chunks", []):
        label = "" if chunk["section"] == "email_body" else f"[{chunk['section']}]\n"
        parts.append(label + chunk["text"])
    return "\n\n".join(parts)


def mask_known(text: str, known: dict[str, str]) -> str:
    """Defence in depth: replace identifiers RH Flow already knows (from the trusted record, e.g. the
    applicant's name, email, phone) that the layer's detectors missed. Each value and each name part
    (3+ letters) becomes its placeholder, case-insensitively, on word boundaries.
    Needed because the layer does not mask names written in a CV header (e.g. "SARRA LAHMAR")."""
    for placeholder, value in known.items():
        value = (value or "").strip()
        if not value:
            continue
        parts = [value] + [p for p in re.split(r"[\s'’-]+", value) if len(p) >= 3 and p.isalpha() and "@" not in value]
        for part in sorted(set(parts), key=len, reverse=True):
            text = re.sub(rf"(?<![\w@.]){re.escape(part)}(?![\w@])", placeholder, text, flags=re.IGNORECASE)
    return text


def to_source(source_id: str, envelope: dict, kind: str, meta: dict | None = None,
              known: dict[str, str] | None = None) -> Source:
    """Adapt an envelope to the agents' Source: the doc hash is what proposals cite."""
    return Source(
        source_id=source_id,
        sha256=envelope["doc_id"].split(":", 1)[1],
        text=mask_known(text_of(envelope), known or {}),
        kind=kind,
        meta={"layer": "service", "doc_id": envelope["doc_id"], "verdict": envelope["scan"]["verdict"],
              "signals": envelope["scan"]["signals"], "language": envelope.get("language"),
              "chunk_ids": [c["chunk_id"] for c in envelope.get("chunks", [])], **(meta or {})},
    )
