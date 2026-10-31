"""① Document Sanitizer, end to end.

file bytes -> real type check -> isolated extraction -> hidden-text filter
           -> Unicode cleanup -> links defanged -> SanitizeResult

Email bodies skip the first two steps (they are already text or HTML).
Any refusal raises RejectedFile with a short reason code.

`on_stage(stage, status, detail)` is called after each step, so the
pipeline can build the report's timeline without duplicating this code.
"""

from collections.abc import Callable
from dataclasses import dataclass

from ingest.report import REJECTION_LABELS, label_for
from ingest.sanitizer.extract import ExtractLimits, extract
from ingest.sanitizer.filetype import FileLimits, RejectedFile, check_file
from ingest.sanitizer.hidden import FilteredText, RemovedItem, filter_spans
from ingest.sanitizer.html_text import html_to_text
from ingest.sanitizer.links import defang_links
from ingest.sanitizer.unicode_clean import clean_text

StageCallback = Callable[[str, str, str], None]


def _ignore(stage: str, status: str, detail: str) -> None:
    pass


@dataclass(frozen=True)
class SanitizeResult:
    file_type: str  # "pdf", "docx" or "email"
    original_text: str  # as a naive extractor would read it (for the report only)
    clean_text: str  # visible, normalized, links defanged: what goes on to PII masking
    removed: list[RemovedItem]
    invisible_chars_removed: int
    links_defanged: int

    @property
    def signals(self) -> list[str]:
        """Contract-style signals, e.g. "hidden_text_removed:2"."""
        signals = []
        if self.removed:
            signals.append(f"hidden_text_removed:{len(self.removed)}")
        if self.invisible_chars_removed:
            signals.append(f"invisible_chars_removed:{self.invisible_chars_removed}")
        if self.links_defanged:
            signals.append(f"links_defanged:{self.links_defanged}")
        return signals


def finish(file_type: str, filtered: FilteredText, on_stage: StageCallback = _ignore) -> SanitizeResult:
    """Last steps, shared by files and emails: Unicode cleanup, then links defanged."""
    hidden = [item for item in filtered.removed if item.why != "html_comment"]
    on_stage("hidden_text", "warn" if hidden else "pass",
             f"{len(hidden)} hidden passage(s) removed" if hidden else "Nothing hidden")

    cleaned = clean_text(filtered.visible_text)  # NFKC first, so fullwidth "ｈｔｔｐ" is caught too
    text, links = defang_links(cleaned.text)
    notes = [f"{cleaned.removed_count} invisible character(s) removed"]
    if links:
        notes.append(f"{links} link(s) made unclickable")
    on_stage("unicode", "warn" if cleaned.removed_count else "pass", ", ".join(notes))

    # Hidden passages are cleaned too: an invisible character inside hidden
    # text ("ig​nore") must not stop the scanner from reading it.
    removed = [RemovedItem(item.why, clean_text(item.text).text, item.location) for item in filtered.removed]
    return SanitizeResult(
        file_type=file_type,
        original_text=filtered.original_text,
        clean_text=text,
        removed=removed,
        invisible_chars_removed=cleaned.removed_count,
        links_defanged=links,
    )


def _reject(on_stage: StageCallback, stage: str, reason: str):
    on_stage(stage, "fail", label_for(REJECTION_LABELS, reason))
    raise RejectedFile(reason)


def sanitize_file(
    data: bytes, file_limits: FileLimits, extract_limits: ExtractLimits, on_stage: StageCallback = _ignore,
) -> SanitizeResult:
    try:
        file_type = check_file(data, file_limits)
    except RejectedFile as rejected:
        _reject(on_stage, "file_check", rejected.reason)
    on_stage("file_check", "pass", f"Real type: {file_type.upper()} ({max(1, len(data) // 1024)} KB)")

    try:
        spans = extract(data, file_type, extract_limits)
    except RejectedFile as rejected:
        _reject(on_stage, "extraction", rejected.reason)
    filtered = filter_spans(spans)
    if filtered.visible_text.strip():
        on_stage("extraction", "pass", f"{len(spans)} text spans read in an isolated process")
    else:
        on_stage("extraction", "warn", "No readable text (scanned image?)")
    return finish(file_type, filtered, on_stage)


def sanitize_email_body(
    body: str, is_html: bool, max_chars: int, on_stage: StageCallback = _ignore,
) -> SanitizeResult:
    if len(body) > max_chars:
        _reject(on_stage, "file_check", "too_much_text")
    on_stage("file_check", "pass", f"Email body ({'HTML' if is_html else 'plain text'})")
    if is_html:
        original, visible, removed = html_to_text(body)
        on_stage("extraction", "pass", "HTML parsed safely, nothing executed")
    else:
        original, visible, removed = body, body, []
        on_stage("extraction", "pass", "Plain text")
    return finish("email", FilteredText(original, visible, removed), on_stage)
