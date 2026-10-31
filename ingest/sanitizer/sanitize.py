"""① Document Sanitizer, end to end.

file bytes -> real type check -> isolated extraction -> hidden-text filter
           -> Unicode cleanup -> links defanged -> SanitizeResult

Email bodies skip the first two steps (they are already text or HTML).
Any refusal raises RejectedFile with a short reason code.
"""

from dataclasses import dataclass

from ingest.sanitizer.extract import ExtractLimits, extract
from ingest.sanitizer.filetype import FileLimits, RejectedFile, check_file
from ingest.sanitizer.hidden import FilteredText, RemovedItem, filter_spans
from ingest.sanitizer.html_text import html_to_text
from ingest.sanitizer.links import defang_links
from ingest.sanitizer.unicode_clean import clean_text


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


def finish(file_type: str, filtered: FilteredText) -> SanitizeResult:
    """Last steps, shared by files and emails: Unicode cleanup, then links defanged."""
    cleaned = clean_text(filtered.visible_text)  # NFKC first, so fullwidth "ｈｔｔｐ" is caught too
    text, links = defang_links(cleaned.text)
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


def sanitize_file(data: bytes, file_limits: FileLimits, extract_limits: ExtractLimits) -> SanitizeResult:
    file_type = check_file(data, file_limits)
    spans = extract(data, file_type, extract_limits)
    return finish(file_type, filter_spans(spans))


def sanitize_email_body(body: str, is_html: bool, max_chars: int) -> SanitizeResult:
    if len(body) > max_chars:
        raise RejectedFile("too_much_text")
    if is_html:
        original, visible, removed = html_to_text(body)
    else:
        original, visible, removed = body, body, []
    return finish("email", FilteredText(original, visible, removed))
