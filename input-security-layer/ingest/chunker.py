"""Split the masked text into chunks the AI layer can cite.

1. Split at CV section headings (Expérience / Experience, Formation / Education, ...).
2. Split long sections into chunks of about 1,000 characters, cutting at a
   line break or a space, never inside a token such as <PERSON_12>.
3. If that would give more chunks than the contract allows (200), use
   bigger chunks (up to the contract's 4,000 characters) instead of failing.

Each chunk later gets chunk_id = sha256 of its exact text (contract rule).
"""

import re

from ingest.contract import MAX_CHUNK_CHARS as CONTRACT_MAX_CHARS
from ingest.contract import MAX_CHUNKS

TARGET_CHUNK_CHARS = 1_000

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


def _cut_point(line: str, size: int) -> int:
    """Where to cut a line longer than `size`: the last space, or else just
    before an unclosed "<" or "[" so a token or marker is never split."""
    space = line.rfind(" ", 0, size + 1)
    if space >= size // 2:
        return space
    opener = max(line.rfind("<", 0, size), line.rfind("[", 0, size))
    closer = max(line.rfind(">", 0, size), line.rfind("]", 0, size))
    return opener if opener > closer and opener > 0 else size


def _split_long(text: str, size: int) -> list[str]:
    """Pack whole lines into chunks of at most `size` characters."""
    chunks, current = [], ""
    for line in text.split("\n"):
        while len(line) > size:
            if current:
                chunks.append(current)
                current = ""
            cut = _cut_point(line, size)
            chunks.append(line[:cut])
            line = line[cut:].lstrip()
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > size:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def _sections(text: str, is_email: bool) -> list[tuple[str, str]]:
    if is_email:
        return [("email_body", text)]
    sections, section, lines = [], "other", []
    for line in text.split("\n"):
        found = _heading(line)
        if found:
            sections.append((section, "\n".join(lines)))
            section, lines = found, []
        lines.append(line)  # the heading line stays with its section
    sections.append((section, "\n".join(lines)))
    return sections


def _chunk(text: str, is_email: bool, size: int) -> list[tuple[str, str]]:
    chunks, seen = [], set()
    for section, body in _sections(text, is_email):
        for piece in _split_long(body, size):
            piece = piece.strip()
            # The contract forbids two chunks with the same id (same text).
            if piece and piece not in seen:
                seen.add(piece)
                chunks.append((section, piece))
    return chunks


def chunk_text(text: str, is_email: bool) -> list[tuple[str, str]]:
    """Return (section, text) pairs, in document order, without duplicates."""
    size = TARGET_CHUNK_CHARS
    while True:
        chunks = _chunk(text, is_email, size)
        if len(chunks) <= MAX_CHUNKS:
            return chunks
        if size >= CONTRACT_MAX_CHARS:
            raise ValueError("text does not fit in the contract's chunk limits")
        size = min(CONTRACT_MAX_CHARS, size * 2)
