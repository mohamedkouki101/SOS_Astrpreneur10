"""Split the masked text into chunks the AI layer can cite.

1. Split at CV section headings (Expérience / Experience, Formation / Education, ...).
2. Split long sections at line breaks, into chunks of about 1,000 characters.

Each chunk later gets chunk_id = sha256 of its exact text (contract rule).
"""

import re

MAX_CHUNK_CHARS = 1_000

SECTION_HEADINGS = {
    "experience": r"exp[ée]riences?(?: professionnelles?)?|(?:work|professional) experience|employment",
    "education": r"formations?|[ée]ducation|parcours acad[ée]mique|dipl[oô]mes?",
    "skills": r"comp[ée]tences?(?: techniques)?|(?:technical )?skills",
    "languages": r"langues|languages",
    "summary": r"profil|r[ée]sum[ée]|summary|profile|about me|[àa] propos(?: de moi)?",
}
HEADINGS = [
    (section, re.compile(rf"[ \t]*(?:{pattern})[ \t]*:?[ \t]*", re.IGNORECASE))
    for section, pattern in SECTION_HEADINGS.items()
]


def _heading(line: str) -> str | None:
    for section, pattern in HEADINGS:
        if pattern.fullmatch(line):
            return section
    return None


def _split_long(text: str) -> list[str]:
    """Pack whole lines into chunks of at most MAX_CHUNK_CHARS; cut a longer line."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > MAX_CHUNK_CHARS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:MAX_CHUNK_CHARS])
            line = line[MAX_CHUNK_CHARS:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def chunk_text(text: str, is_email: bool) -> list[tuple[str, str]]:
    """Return (section, text) pairs, in document order, without duplicates."""
    if is_email:
        sections = [("email_body", text)]
    else:
        sections, section, lines = [], "other", []
        for line in text.split("\n"):
            found = _heading(line)
            if found:
                sections.append((section, "\n".join(lines)))
                section, lines = found, []
            lines.append(line)  # the heading line stays with its section
        sections.append((section, "\n".join(lines)))

    chunks, seen = [], set()
    for section, body in sections:
        for piece in _split_long(body):
            piece = piece.strip()
            # The contract forbids two chunks with the same id (same text).
            if piece and piece not in seen:
                seen.add(piece)
                chunks.append((section, piece))
    return chunks
