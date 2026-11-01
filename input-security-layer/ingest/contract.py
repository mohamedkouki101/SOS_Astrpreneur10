"""The envelope sent to the AI layer (contract v1.1).

Every model is:
- strict: no silent type conversion ("6200" or 6200.0 is not accepted as an int),
- frozen: fields cannot be reassigned (lists can still be changed in place,
  which is why sign_document validates the whole document again before signing),
- closed: unknown fields are rejected (extra="forbid").

Formats are checked with anchored patterns, so a malformed ID or token is
rejected here instead of reaching the AI layer.
"""

import hashlib
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from ingest import PIPELINE_VERSION, SCHEMA_VERSION

# --- Reusable string formats ---
Sha256Id = Annotated[str, StringConstraints(pattern=r"^sha256:[0-9a-f]{64}$")]
HmacRef = Annotated[str, StringConstraints(pattern=r"^hmac:[0-9a-f]{64}$")]
Signature = Annotated[str, StringConstraints(pattern=r"^hmac-sha256:[0-9a-f]{64}$")]
MaskToken = Annotated[str, StringConstraints(pattern=r"^<[A-Z_]+_[1-9][0-9]{0,3}>$")]
# Signals look like "hidden_text_removed:2" or "rule:ignore_previous_fr".
ScanSignal = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,40}(:[a-z0-9_.-]{1,40})?$")]
# Email Message-ID headers come from outside: printable ASCII only, bounded length.
MessageId = Annotated[str, StringConstraints(pattern=r"^[!-~]{1,998}$")]
# Frappe document names, e.g. "HR-OPN-0004".
FrappeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9-]{1,64}$")]
# One fixed text form for timestamps, so the signature is stable.
UtcTimestamp = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")]

SourceType = Literal["upload_cv", "email_body", "email_attachment"]
Verdict = Literal["clean", "suspicious", "quarantined"]
Section = Literal["experience", "education", "skills", "languages", "summary", "other", "email_body"]
ProtectedAttribute = Literal[
    "AGE", "BIRTH_DATE", "GENDER", "MARITAL_STATUS", "NATIONALITY", "RELIGION", "PHOTO"
]

MAX_CHUNK_CHARS = 4_000
MAX_CHUNKS = 200


def sha256_id(data: bytes) -> str:
    """One helper for both IDs in the contract:
    doc_id   = sha256_id(raw_file_bytes)
    chunk_id = sha256_id(chunk_text.encode("utf-8"))
    """
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _no_duplicates(items: list, name: str) -> None:
    if len(set(items)) != len(items):
        raise ValueError(f"{name} must not contain duplicates")


class ContractModel(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")


class Source(ContractModel):
    type: SourceType
    message_id: MessageId | None
    in_reply_to: MessageId | None
    file_ext: Literal["pdf", "docx"] | None
    received_at: UtcTimestamp
    sender_ref: HmacRef | None

    @field_validator("received_at")
    @classmethod
    def is_real_date(cls, value: str) -> str:
        # The pattern accepts "2026-13-45T99:00:00Z"; this rejects impossible dates.
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
        return value

    @model_validator(mode="after")
    def fields_match_type(self) -> "Source":
        if self.type == "upload_cv":
            # A recruiter upload has no email behind it.
            if self.message_id or self.in_reply_to or self.sender_ref:
                raise ValueError("upload_cv must not have message_id, in_reply_to or sender_ref")
        else:
            if not self.message_id or not self.sender_ref:
                raise ValueError("email sources need message_id and sender_ref")
        # Only files have an extension; an email body does not.
        if (self.type == "email_body") != (self.file_ext is None):
            raise ValueError("file_ext must be null for email_body and set for files")
        return self


class JobOpening(ContractModel):
    id: FrappeName
    origin: Literal["recruiter_upload", "email_hint"]


class Scan(ContractModel):
    verdict: Verdict
    risk_score_bp: int = Field(ge=0, le=10_000)  # basis points: integer, never a float
    signals: list[ScanSignal] = Field(max_length=100)


class Pii(ContractModel):
    masked: bool
    token_scope: Literal["doc"]
    tokens: list[MaskToken] = Field(max_length=1_000)
    protected_removed: list[ProtectedAttribute]

    @model_validator(mode="after")
    def unique_entries(self) -> "Pii":
        _no_duplicates(self.tokens, "pii.tokens")
        _no_duplicates(self.protected_removed, "pii.protected_removed")
        return self


class Chunk(ContractModel):
    chunk_id: Sha256Id
    section: Section
    text: str = Field(min_length=1, max_length=MAX_CHUNK_CHARS)

    @model_validator(mode="after")
    def id_matches_text(self) -> "Chunk":
        # Same check the AI validator runs: the ID must prove the text is unchanged.
        if self.chunk_id != sha256_id(self.text.encode("utf-8")):
            raise ValueError("chunk_id does not match sha256 of text")
        return self


class IngestedDocument(ContractModel):
    schema_version: Literal[SCHEMA_VERSION]
    doc_id: Sha256Id
    candidate_ref: HmacRef | None  # null when no email was found in the CV or sender
    source: Source
    job_opening: JobOpening | None
    language: Literal["fr", "en", "unknown"]
    trust: Literal["untrusted"]  # can never be set to anything else
    scan: Scan
    pii: Pii
    chunks: list[Chunk] = Field(max_length=MAX_CHUNKS)
    pipeline_version: Literal[PIPELINE_VERSION]
    signature: Signature | None = None  # filled in by ingest.signing.sign_document

    @model_validator(mode="after")
    def cross_checks(self) -> "IngestedDocument":
        # A quarantined document must never carry text toward the AI.
        if self.scan.verdict == "quarantined" and self.chunks:
            raise ValueError("quarantined documents must have no chunks")
        # Fail closed: text only leaves this service after PII masking.
        if self.chunks and not self.pii.masked:
            raise ValueError("chunks require pii.masked = true")
        # One chunk_id must point to exactly one chunk, so citations are unambiguous.
        _no_duplicates([chunk.chunk_id for chunk in self.chunks], "chunk_id")
        # A trusted origin is only possible when a recruiter uploaded the file.
        if self.job_opening is not None:
            expected = "recruiter_upload" if self.source.type == "upload_cv" else "email_hint"
            if self.job_opening.origin != expected:
                raise ValueError(f"job_opening.origin must be {expected} for {self.source.type}")
        return self
