"""Text extraction, run in an isolated child process. Never imported by the service.

Started by ingest.sanitizer.extract as:
    python -I _extract_child.py <file_type> <limits as JSON>
File bytes arrive on stdin. One JSON line goes to stdout:
    {"ok": true, "spans": [...]}   or   {"ok": false, "reason": "<code>"}

This file imports only the standard library, PyMuPDF and python-docx:
none of the service code, and it never sees any secret. If a hostile file
crashes or exhausts this process, only this process dies.
"""

import io
import json
import sys
import zipfile


class Reject(Exception):
    """A clean refusal with a short reason code."""


class TextBudget:
    """Counts characters while extracting, so an oversized document is
    stopped early instead of first filling memory."""

    def __init__(self, limit: int):
        self.limit = limit
        self.used = 0

    def add(self, text: str) -> str:
        self.used += len(text)
        if self.used > self.limit:
            raise Reject("too_much_text")  # no real CV is this long
        return text


def limit_resources(memory_mb: int, cpu_seconds: int) -> None:
    # OS-enforced limits. The `resource` module exists on Linux (the Docker
    # target) but not on Windows, where only the parent's timeout applies.
    try:
        import resource
    except ImportError:
        return
    memory = memory_mb * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))


# --- PDF ---

def extract_pdf(data: bytes, limits: dict, budget: TextBudget) -> list[dict]:
    import pymupdf

    pymupdf.TOOLS.mupdf_display_errors(False)  # keep stdout for our JSON only
    pymupdf.TOOLS.mupdf_display_warnings(False)

    spans = []
    line_number = 0  # lets the parent rebuild the text line by line
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        if doc.needs_pass or doc.is_encrypted:
            raise Reject("pdf_encrypted")
        if doc.page_count > limits["max_pdf_pages"]:
            raise Reject("too_many_pages")
        for page_number, page in enumerate(doc, start=1):
            page_box = [page.rect.width, page.rect.height]
            # sort=True: blocks in reading order (top to bottom, left to right).
            for block in page.get_text("dict", sort=True)["blocks"]:
                for line in block.get("lines", []):
                    line_number += 1
                    for span in line["spans"]:
                        spans.append({
                            "text": budget.add(span["text"]),
                            "page": page_number,
                            "line": line_number,
                            "size": span["size"],
                            "color": span["color"],  # sRGB as an integer 0xRRGGBB
                            # Required, never defaulted: a missing value must
                            # fail, not silently count as "visible".
                            "alpha": span["alpha"],
                            "char_flags": span["char_flags"],  # filled / stroked bits
                            "bbox": list(span["bbox"]),
                            "page_box": page_box,
                        })
    return spans


# --- DOCX ---

def check_real_zip_size(data: bytes, max_total: int) -> None:
    """Decompress every entry and count the real bytes, whatever the zip claims.

    This is the real zip-bomb defense: sizes in the zip's file list can be
    faked, the bytes coming out of the decompressor cannot.
    """
    total = 0
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for entry in archive.infolist():
                # open() also refuses an entry whose local header name differs
                # from the file list, so what was checked is what is read.
                with archive.open(entry) as part:
                    while chunk := part.read(64 * 1024):
                        total += len(chunk)
                        if total > max_total:
                            raise Reject("zip_bomb")
    except zipfile.BadZipFile:
        raise Reject("bad_zip") from None


def _font_hidden(font):
    return font.hidden


def _font_size(font):
    return font.size.pt if font.size is not None else None


def _font_color(font):
    color = font.color
    return str(color.rgb) if color is not None and color.rgb is not None else None


def _effective(run, paragraph, read):
    """A font property as Word resolves it: the run itself first, then its
    character style, then the paragraph style, each with the styles it is
    based on. Hiding text through a style is a common trick, so all count."""
    value = read(run.font)
    if value is not None:
        return value
    for style in (run.style, paragraph.style):
        for _ in range(20):  # bounded: a crafted file could loop styles
            if style is None:
                break
            value = read(style.font)
            if value is not None:
                return value
            style = style.base_style
    return None


def _paragraph_spans(paragraph, part: str, index: int, budget: TextBudget) -> list[dict]:
    from docx.text.hyperlink import Hyperlink

    spans = []
    # iter_inner_content also yields hyperlinks (e.g. a CV's email or
    # LinkedIn link), whose runs `paragraph.runs` would silently skip.
    for item in paragraph.iter_inner_content():
        runs = item.runs if isinstance(item, Hyperlink) else [item]
        for run in runs:
            spans.append({
                "text": budget.add(run.text),
                "part": part,
                "paragraph": index,
                "size": _effective(run, paragraph, _font_size),
                "color": _effective(run, paragraph, _font_color),
                "hidden": bool(_effective(run, paragraph, _font_hidden)),
            })
    return spans


def _walk(container, part: str, counter: list[int], budget: TextBudget) -> list[dict]:
    """Paragraphs and tables in document order, including nested tables."""
    from docx.table import Table, _Cell

    spans = []
    for item in container.iter_inner_content():
        if isinstance(item, Table):
            # Walk the XML cells directly: Row.cells repeats a merged cell once
            # per column it spans, which would duplicate (or, nested, multiply) text.
            for row in item._tbl.tr_lst:
                for cell in row.tc_lst:
                    spans += _walk(_Cell(cell, item), part, counter, budget)
        else:
            counter[0] += 1
            spans += _paragraph_spans(item, part, counter[0], budget)
    return spans


def extract_docx(data: bytes, limits: dict, budget: TextBudget) -> list[dict]:
    check_real_zip_size(data, limits["max_zip_uncompressed_bytes"])

    import docx

    document = docx.Document(io.BytesIO(data))
    counter = [0]
    spans = _walk(document, "body", counter, budget)
    for section in document.sections:
        for part, items in (
            ("header", (section.header, section.first_page_header, section.even_page_header)),
            ("footer", (section.footer, section.first_page_footer, section.even_page_footer)),
        ):
            for header_or_footer in items:
                # A linked one is the previous section's: reading it again would duplicate it.
                if not header_or_footer.is_linked_to_previous:
                    spans += _walk(header_or_footer, part, counter, budget)
    return spans


EXTRACTORS = {"pdf": extract_pdf, "docx": extract_docx}


def run(file_type: str, limits: dict, data: bytes) -> dict:
    try:
        if len(data) > limits["max_file_bytes"]:
            raise Reject("too_large")
        budget = TextBudget(limits["max_text_chars"])
        return {"ok": True, "spans": EXTRACTORS[file_type](data, limits, budget)}
    except Reject as reject:
        return {"ok": False, "reason": str(reject)}
    except MemoryError:
        return {"ok": False, "reason": "extract_memory"}
    except Exception:
        # Never echo the error: it could contain file content.
        return {"ok": False, "reason": "extract_failed"}


def main() -> None:
    file_type = sys.argv[1]
    limits = json.loads(sys.argv[2])
    limit_resources(limits["memory_mb"], limits["timeout_s"])
    data = sys.stdin.buffer.read(limits["max_file_bytes"] + 1)
    # ensure_ascii (the default) keeps the output plain ASCII on every platform.
    sys.stdout.write("\n" + json.dumps(run(file_type, limits, data)) + "\n")


if __name__ == "__main__":
    main()
