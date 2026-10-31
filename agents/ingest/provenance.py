"""Stage 4 - Provenance tagger: source ID, SHA-256 of the original bytes, candidate ID, trust=untrusted."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Provenance:
    source_id: str
    sha256: str
    candidate_id: str | None
    trust: str = "untrusted"
    ingested_at: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def sha256(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def tag(source_id: str, original: bytes | str, candidate_id: str | None = None) -> Provenance:
    return Provenance(
        source_id=source_id,
        sha256=sha256(original),
        candidate_id=candidate_id,
        ingested_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
