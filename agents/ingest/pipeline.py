"""The fixed 4-stage pipeline every piece of external content goes through (even in tests).

    1 sanitize -> 2 mask PII -> 3 scan for injection -> 4 tag provenance

Only the result's `text` (visible, masked) may ever reach the LLM, and only when status == "ok".
Quarantined items return no text at all and raise an alert.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from agents.config import get_settings
from agents.ingest import injection, pii, provenance, sanitizer
from agents.orchestrator import audit


@dataclass
class IngestResult:
    status: str                                  # "ok" | "quarantined" | "rejected"
    source_id: str
    sha256: str
    text: str = ""                               # masked visible text (empty unless ok)
    headers: dict[str, str] = field(default_factory=dict)   # masked
    findings: list[str] = field(default_factory=list)
    injection_hits: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"status": self.status, "source_id": self.source_id, "sha256": self.sha256,
                "findings": self.findings, "injection_hits": self.injection_hits}


@lru_cache(maxsize=1)
def token_store() -> pii.TokenStore:
    return pii.TokenStore(get_settings().var_dir / "token_map.json")


def ingest_bytes(source_id: str, filename: str, data: bytes, *, known_pii: dict[str, str] | None = None,
                 candidate_id: str | None = None) -> IngestResult:
    prov = provenance.tag(source_id, data, candidate_id)
    try:
        doc = sanitizer.sanitize(filename, data)                                     # stage 1
    except sanitizer.Rejected as exc:
        audit.log("ingest_rejected", source_id=source_id, sha256=prov.sha256, reason=str(exc))
        return IngestResult("rejected", source_id, prov.sha256, findings=[str(exc)], provenance=prov.as_dict())

    store = token_store()
    masked = pii.mask(doc.text, store, known_pii)                                     # stage 2
    masked_hidden = pii.mask(doc.hidden_text, store, known_pii).text if doc.hidden_text else ""
    masked_headers = {k: pii.mask(v, store, known_pii).text for k, v in doc.headers.items()}
    store.save()

    scan = injection.scan(masked.text + "\n" + masked_headers.get("Subject", ""), masked_hidden)   # stage 3
    findings = doc.findings + ([f"protected_lines_removed:{masked.removed_protected_lines}"]
                               if masked.removed_protected_lines else [])
    if scan.quarantined:
        audit.log("quarantine", source_id=source_id, sha256=prov.sha256, candidate_id=candidate_id, hits=scan.hits)
        audit.alert("injection_quarantine", source_id=source_id, sha256=prov.sha256, hits=scan.hits)
        return IngestResult("quarantined", source_id, prov.sha256, findings=findings,
                            injection_hits=scan.hits, provenance=prov.as_dict())

    return IngestResult("ok", source_id, prov.sha256, text=masked.text, headers=masked_headers,   # stage 4
                        findings=findings, provenance=prov.as_dict())


def ingest_text(source_id: str, text: str, *, known_pii: dict[str, str] | None = None,
                candidate_id: str | None = None) -> IngestResult:
    """Free-text fields from HR Flow records (cover letters, leave reasons, grievances)."""
    return ingest_bytes(source_id, f"{source_id}.txt", (text or "").encode("utf-8"),
                        known_pii=known_pii, candidate_id=candidate_id)
