"""The inspection report: one JSON document per file or email, for the dashboard.

It shows what happened at each stage: original vs cleaned vs masked text,
what was removed or masked and why, the verdict with its score breakdown,
and the hashes. It contains personal data (original text and values), so it
is served only to HR on the internal API and never sent to the AI.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict

StageName = Literal["file_check", "extraction", "hidden_text", "unicode", "pii", "injection", "chunking", "signing"]
StageStatus = Literal["pass", "warn", "fail", "skipped"]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stage(_Model):
    stage: StageName
    status: StageStatus
    detail: str
    ms: int = 0


class Rejection(_Model):
    code: str
    label: str


class ReportSource(_Model):
    type: Literal["upload_cv", "email_body", "email_attachment"]
    file_type: Literal["pdf", "docx", "email", "unknown"]
    size_bytes: int


class Reason(_Model):
    code: str
    label: str
    points: int


class ReportText(_Model):
    original: str  # everything, as a naive extractor would have read it
    cleaned: str  # visible text, Unicode-cleaned, links defanged
    masked: str  # PII masked
    sent: str  # exactly what the AI layer receives (chunks joined); "" if nothing


class RemovedPassage(_Model):
    why: str
    label: str
    text: str
    location: str


class MaskedValue(_Model):
    token: str
    entity: str
    original: str
    count: int


class ProtectedValue(_Model):
    attribute: str
    original: str


class InjectionMatch(_Model):
    rule: str
    label: str
    text: str
    where: Literal["visible", "hidden"]


class Hashes(_Model):
    doc_id: str
    chunk_ids: list[str]
    signature: str | None


class InspectionReport(_Model):
    report_version: Literal["1.0"] = "1.0"
    doc_id: str
    received_at: str
    source: ReportSource
    job_opening: dict | None
    status: Literal["accepted", "rejected", "error"]
    rejection: Rejection | None
    verdict: Literal["clean", "suspicious", "quarantined"] | None  # None when rejected
    risk_score_bp: int
    language: str | None
    reasons: list[Reason]
    stages: list[Stage]
    text: ReportText
    removed: list[RemovedPassage]
    masked: list[MaskedValue]
    protected_removed: list[ProtectedValue]
    injection_matches: list[InjectionMatch]
    hashes: Hashes
    released_to_ai: bool  # True only for accepted, non-quarantined documents


# --- Readable labels for every code (English; the dashboard may translate) ---

REJECTION_LABELS = {
    "empty": "Empty file",
    "too_large": "File too large",
    "unsupported_type": "Not a PDF or Word (DOCX) file",
    "bad_zip": "Damaged Word file",
    "zip_too_many_entries": "Word file with too many internal parts",
    "zip_duplicate_entry": "Word file with duplicate parts (parser confusion trick)",
    "zip_encrypted": "Encrypted Word file",
    "zip_bad_path": "Word file with suspicious internal paths",
    "zip_bomb": "Zip bomb: expands to a huge size",
    "not_docx": "ZIP archive, not a Word document",
    "docx_macro": "Contains macros",
    "docx_active_content": "Contains ActiveX controls or embedded objects",
    "docx_bad_content_types": "Malformed Word file",
    "pdf_encrypted": "Password-protected PDF",
    "too_many_pages": "Too many pages",
    "too_much_text": "Too much text",
    "extract_timeout": "Took too long to read (possible attack)",
    "extract_memory": "Used too much memory to read (possible attack)",
    "extract_failed": "Could not be read safely",
    "internal_error": "Processing error",
}

REMOVED_LABELS = {
    "white_text": "White text on a white page",
    "tiny_text": "Text too small to read",
    "transparent_text": "Transparent text",
    "invisible_text": "Invisible text (PDF render mode)",
    "clipped_text": "Text cut away by a clipping path",
    "off_page": "Text placed outside the page",
    "word_hidden": "Text marked as hidden in Word",
    "html_hidden": "Hidden HTML element",
    "html_comment": "HTML comment",
}


def label_for(table: dict[str, str], code: str) -> str:
    return table.get(code, code.replace("_", " ").capitalize())
