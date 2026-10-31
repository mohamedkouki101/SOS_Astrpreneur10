"""Hidden-text filter for PDF and DOCX spans.

Hidden text is what a human reviewer cannot see but an AI reading the raw
text would: the classic way to slip instructions into a CV. Each span is
either kept (visible) or removed with the reason why, so the inspection
report can show exactly what was taken out.
"""

from dataclasses import dataclass

from ingest.sanitizer.extract import DocxSpan, PdfSpan

MIN_FONT_PT = 4  # below this, text is unreadable on screen or paper
MIN_ALPHA = 26  # out of 255: under ~10% opacity
NEAR_WHITE = 0xF0  # every RGB channel at or above this counts as white
PDF_FILLED = 16  # MuPDF char_flags bits (checked against real files):
PDF_STROKED = 32  # neither bit set = text drawn in invisible mode
PDF_CLIPPED = 64  # glyph cut away by a clipping path


@dataclass(frozen=True)
class RemovedItem:
    """One removed passage, as shown in the inspection report."""

    why: str  # e.g. "white_text", "tiny_text", "word_hidden"
    text: str
    location: str  # e.g. "page 1", "paragraph 3", "html"


def _is_near_white(red: int, green: int, blue: int) -> bool:
    return min(red, green, blue) >= NEAR_WHITE


def pdf_hidden_reason(span: PdfSpan) -> str | None:
    """Why this PDF span is hidden, or None if a reader can see it."""
    if not span.char_flags & (PDF_FILLED | PDF_STROKED):
        return "invisible_text"
    if span.char_flags & PDF_CLIPPED:
        return "clipped_text"
    if span.alpha < MIN_ALPHA:
        return "transparent_text"
    if span.size < MIN_FONT_PT:
        return "tiny_text"
    color = span.color
    if _is_near_white(color >> 16 & 0xFF, color >> 8 & 0xFF, color & 0xFF):
        return "white_text"  # assumes a white page, the normal case for CVs
    x0, y0, x1, y1 = span.bbox
    width, height = span.page_box
    if x1 <= 0 or y1 <= 0 or x0 >= width or y0 >= height:
        return "off_page"
    return None


def docx_hidden_reason(span: DocxSpan) -> str | None:
    """Why this DOCX run is hidden, or None if a reader can see it."""
    if span.hidden:
        return "word_hidden"  # Word's "Hidden" font option, directly or via a style
    if span.size is not None and span.size < MIN_FONT_PT:
        return "tiny_text"
    if span.color is not None:
        try:
            red, green, blue = (int(span.color[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return None  # not an RGB value (e.g. a theme color): treat as visible
        if _is_near_white(red, green, blue):
            return "white_text"
    return None


@dataclass(frozen=True)
class FilteredText:
    original_text: str  # everything, as a naive extractor would have read it
    visible_text: str  # what a human sees
    removed: list[RemovedItem]


def filter_spans(spans: list[PdfSpan] | list[DocxSpan]) -> FilteredText:
    """Rebuild the text line by line (PDF) or paragraph by paragraph (DOCX),
    keeping visible spans and recording the hidden ones."""
    original_lines: list[str] = []
    visible_lines: list[str] = []
    removed: list[RemovedItem] = []
    current_key = None

    for span in spans:
        if isinstance(span, PdfSpan):
            key, location, reason = (span.page, span.line), f"page {span.page}", pdf_hidden_reason(span)
        else:
            key, location, reason = (span.part, span.paragraph), _docx_location(span), docx_hidden_reason(span)

        if key != current_key:  # a new line or paragraph starts
            original_lines.append("")
            visible_lines.append("")
            current_key = key
        original_lines[-1] += span.text

        if reason is None or not span.text.strip():
            # Whitespace cannot hide anything: keep it so words stay apart.
            visible_lines[-1] += span.text
            continue
        # Merge consecutive hidden spans with the same reason into one item.
        if removed and removed[-1].why == reason and removed[-1].location == location:
            last = removed.pop()
            removed.append(RemovedItem(reason, last.text + span.text, location))
        else:
            removed.append(RemovedItem(reason, span.text, location))

    return FilteredText(
        original_text="\n".join(original_lines),
        visible_text="\n".join(line for line in visible_lines if line.strip()),
        removed=removed,
    )


def _docx_location(span: DocxSpan) -> str:
    return f"paragraph {span.paragraph}" if span.part == "body" else span.part
