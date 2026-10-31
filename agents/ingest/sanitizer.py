"""Stage 1 - Sanitizer.

Type/size allowlist (PDF, DOCX, EML, plain text), active-content check, text extraction that
separates VISIBLE text from HIDDEN text (white, tiny or off-page characters in PDFs; hidden or
white runs in DOCX; display:none / comments in HTML email), and link stripping.

Hidden text is never silently dropped: it is returned separately so stage 3 can scan it and the
item can be quarantined. Malware scanning is a hook (scanner not chosen yet, see CLAUDE.md).
"""

from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser

MAX_BYTES = 5 * 1024 * 1024
ALLOWED = {".pdf", ".docx", ".eml", ".txt"}
URL_RE = re.compile(r"(https?://|www\.)[^\s<>\"')]+", re.I)
ACTIVE_PDF_MARKERS = (b"/JavaScript", b"/JS ", b"/Launch", b"/EmbeddedFile", b"/OpenAction", b"/AA ")
MIN_VISIBLE_FONT = 4.0


class Rejected(ValueError):
    """The file is not accepted at all (wrong type, too big, active content, unparsable)."""


@dataclass
class SanitizedDoc:
    kind: str
    text: str                                   # visible text, links removed
    hidden_text: str = ""                       # text a human reader would not see
    headers: dict[str, str] = field(default_factory=dict)
    findings: list[str] = field(default_factory=list)


def malware_scan(data: bytes) -> str | None:
    """Hook for a malware scanner (TODO: scanner not chosen). Returns a finding or None."""
    return None


def _strip_links(text: str, findings: list[str]) -> str:
    text, n = URL_RE.subn("[link removed]", text)
    if n:
        findings.append(f"links_removed:{n}")
    return text


def _is_white(color) -> bool:
    if color is None:
        return False
    vals = list(color) if isinstance(color, (list, tuple)) else [color]
    vals = [v for v in vals if isinstance(v, (int, float))]
    if not vals:
        return False
    if len(vals) == 4:                       # CMYK: white = no ink
        return all(v <= 0.05 for v in vals)
    return all(v >= 0.95 for v in vals)      # gray or RGB


def _pdf(data: bytes, findings: list[str]) -> tuple[str, str]:
    import pdfplumber

    if not data.startswith(b"%PDF"):
        raise Rejected("not a PDF")
    for marker in ACTIVE_PDF_MARKERS:
        if marker in data:
            raise Rejected(f"active content in PDF ({marker.decode().strip()})")
    visible_pages, hidden_chars = [], []
    try:
        with pdfplumber.open(io.BytesIO(data)) as pdf:
            for page in pdf.pages:
                w, h = page.width, page.height

                def hidden(obj, w=w, h=h) -> bool:
                    return (
                        _is_white(obj.get("non_stroking_color"))
                        or float(obj.get("size", 12)) < MIN_VISIBLE_FONT
                        or obj["x1"] < 0 or obj["x0"] > w or obj["bottom"] < 0 or obj["top"] > h
                    )

                for ch in page.chars:
                    if hidden(ch):
                        hidden_chars.append(ch["text"])
                kept = page.filter(lambda o: o.get("object_type") != "char" or not hidden(o))
                visible_pages.append(kept.extract_text() or "")
    except Rejected:
        raise
    except Exception as exc:  # unparsable / malformed
        raise Rejected(f"PDF could not be parsed ({type(exc).__name__})") from exc
    return "\n".join(visible_pages), "".join(hidden_chars)


def _docx(data: bytes, findings: list[str]) -> tuple[str, str]:
    import docx
    from docx.shared import Pt

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = z.namelist()
    except zipfile.BadZipFile as exc:
        raise Rejected("not a DOCX") from exc
    if any("vbaProject" in n for n in names):
        raise Rejected("macros in DOCX")
    document = docx.Document(io.BytesIO(data))
    visible, hidden = [], []
    for para in document.paragraphs:
        line_v, line_h = [], []
        for run in para.runs:
            color = run.font.color.rgb if run.font.color is not None and run.font.color.type else None
            tiny = run.font.size is not None and run.font.size < Pt(MIN_VISIBLE_FONT)
            if run.font.hidden or tiny or str(color).upper() == "FFFFFF":
                line_h.append(run.text)
            else:
                line_v.append(run.text)
        visible.append("".join(line_v))
        hidden.append("".join(line_h))
    return "\n".join(visible), " ".join(h for h in hidden if h)


def _html_to_text(html: str) -> tuple[str, str]:
    from bs4 import BeautifulSoup, Comment

    soup = BeautifulSoup(html, "html.parser")
    hidden = [c.strip() for c in soup.find_all(string=lambda s: isinstance(s, Comment))]
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    hide_re = re.compile(
        r"display\s*:\s*none|visibility\s*:\s*hidden|font-size\s*:\s*[01](px|pt)?\b|color\s*:\s*#?f{3}(f{3})?\b", re.I
    )
    for tag in soup.find_all(style=hide_re):
        hidden.append(tag.get_text(" ", strip=True))
        tag.decompose()
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True), " ".join(h for h in hidden if h)


def _eml(data: bytes, findings: list[str]) -> tuple[str, str, dict[str, str]]:
    msg = BytesParser(policy=policy.default).parsebytes(data)
    headers = {k: str(msg.get(k, "")) for k in ("From", "To", "Subject", "Date", "Message-ID")}
    plain = msg.get_body(preferencelist=("plain",))
    html = msg.get_body(preferencelist=("html",))
    hidden = ""
    if plain is not None:
        text = plain.get_content()
        _, hidden_from_comments = _html_to_text(text) if "<!--" in text else ("", "")
        text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
        hidden = hidden_from_comments
    else:
        text = ""
    if html is not None:
        html_visible, html_hidden = _html_to_text(html.get_content())
        text = text or html_visible
        hidden = " ".join(h for h in (hidden, html_hidden) if h)
    attachments = [p.get_filename() for p in msg.iter_attachments()]
    if attachments:
        findings.append(f"attachments_not_processed:{len(attachments)}")
    return text, hidden, headers


def sanitize(filename: str, data: bytes) -> SanitizedDoc:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ALLOWED:
        raise Rejected(f"file type {ext or '?'} not allowed")
    if len(data) > MAX_BYTES:
        raise Rejected("file too large")
    findings: list[str] = []
    if (hit := malware_scan(data)) is not None:
        raise Rejected(f"malware scan: {hit}")
    headers: dict[str, str] = {}
    if ext == ".pdf":
        text, hidden = _pdf(data, findings)
    elif ext == ".docx":
        text, hidden = _docx(data, findings)
    elif ext == ".eml":
        text, hidden, headers = _eml(data, findings)
    else:
        text, hidden = data.decode("utf-8", errors="replace"), ""
    text = _strip_links(text, findings)
    if hidden.strip():
        findings.append(f"hidden_text_chars:{len(hidden.strip())}")
    return SanitizedDoc(kind=ext[1:], text=text, hidden_text=hidden.strip(), headers=headers, findings=findings)
