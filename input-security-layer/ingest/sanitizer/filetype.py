"""Real file type detection and pre-parsing limits.

Only the bytes are trusted. The filename, extension and declared Content-Type
are chosen by the sender, so this module never looks at them.

For DOCX (a zip archive) the zip's file list is checked first, without
decompressing anything. Only then is one small part read: [Content_Types].xml,
with a hard cap on how many bytes are decompressed. Sizes in the file list can
be faked, so text extraction enforces its own decompression limit again.
"""

import io
import re
import zipfile
from dataclasses import dataclass
from typing import Literal

FileType = Literal["pdf", "docx"]

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"
REQUIRED_DOCX_ENTRIES = ("[Content_Types].xml", "word/document.xml")
# Embedded objects and ActiveX controls can carry code: never needed in a CV.
ACTIVE_CONTENT_DIRS = ("word/activex/", "word/embeddings/")
MACRO_MARKERS = (b"vbaproject", b"macroenabled")  # searched in lowercased content types
MAX_CONTENT_TYPES_BYTES = 64 * 1024
DRIVE_LETTER = re.compile(r"^[A-Za-z]:")


class RejectedFile(Exception):
    """The file is refused. `reason` is a short code for the audit log.

    It never contains the filename or any file content.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class FileLimits:
    max_file_bytes: int
    max_zip_entries: int
    max_zip_uncompressed_bytes: int
    max_zip_ratio: int

    @classmethod
    def from_settings(cls, settings) -> "FileLimits":
        return cls(
            max_file_bytes=settings.max_file_bytes,
            max_zip_entries=settings.max_zip_entries,
            max_zip_uncompressed_bytes=settings.max_zip_uncompressed_bytes,
            max_zip_ratio=settings.max_zip_ratio,
        )


def check_file(data: bytes, limits: FileLimits) -> FileType:
    """Return "pdf" or "docx", or raise RejectedFile. Cheapest checks first."""
    if not data:
        raise RejectedFile("empty")
    if len(data) > limits.max_file_bytes:
        raise RejectedFile("too_large")

    # Magic bytes must be at offset 0. PDF readers tolerate junk before the
    # header, but refusing it makes "two formats in one file" tricks harder.
    if data.startswith(PDF_MAGIC):
        return "pdf"
    if data.startswith(ZIP_MAGIC):
        _check_docx(data, limits)
        return "docx"
    raise RejectedFile("unsupported_type")  # .doc, RTF, HTML, executables, ...


def _check_docx(data: bytes, limits: FileLimits) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))  # reads the file list only
    except Exception:
        # Any parsing error on hostile input means "reject", never "crash".
        raise RejectedFile("bad_zip") from None

    with archive:
        _check_entries(archive.infolist(), len(data), limits)
        content_types = _read_capped(archive, "[Content_Types].xml", MAX_CONTENT_TYPES_BYTES)

    # Word finds macros by their declared type, not their filename, so a
    # renamed vbaProject.bin is still caught here. Plain substring search:
    # no XML parser runs on hostile input.
    lowered = content_types.lower()
    if any(marker in lowered for marker in MACRO_MARKERS):
        raise RejectedFile("docx_macro")


def _check_entries(entries: list[zipfile.ZipInfo], file_size: int, limits: FileLimits) -> None:
    if len(entries) > limits.max_zip_entries:
        raise RejectedFile("zip_too_many_entries")

    # orig_filename is the raw name; zipfile cuts `filename` at the first NUL byte.
    raw_names = [entry.orig_filename for entry in entries]
    # Word treats names case-insensitively, so "Word/Document.xml" duplicates
    # "word/document.xml". Different parsers may read different copies.
    if len({_normalize(name) for name in raw_names}) != len(raw_names):
        raise RejectedFile("zip_duplicate_entry")

    total_uncompressed = 0
    for entry in entries:
        name = _normalize(entry.orig_filename)
        if entry.flag_bits & 0x1:
            raise RejectedFile("zip_encrypted")
        if _unsafe_name(entry.orig_filename):
            raise RejectedFile("zip_bad_path")
        if name.rsplit("/", 1)[-1] == "vbaproject.bin":
            raise RejectedFile("docx_macro")
        if name.startswith(ACTIVE_CONTENT_DIRS):
            raise RejectedFile("docx_active_content")
        total_uncompressed += entry.file_size

    if total_uncompressed > limits.max_zip_uncompressed_bytes:
        raise RejectedFile("zip_bomb")
    # Integer comparison (no division, no floats): uncompressed / file size > ratio.
    if total_uncompressed > limits.max_zip_ratio * file_size:
        raise RejectedFile("zip_bomb")

    present = set(raw_names)
    for required in REQUIRED_DOCX_ENTRIES:
        if required not in present:
            raise RejectedFile("not_docx")  # a zip, but not a Word document


def _read_capped(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    """Decompress at most limit + 1 bytes, whatever size the zip claims."""
    try:
        with archive.open(name) as part:
            content = part.read(limit + 1)
    except Exception:
        raise RejectedFile("bad_zip") from None
    if len(content) > limit:
        raise RejectedFile("docx_bad_content_types")
    return content


def _normalize(name: str) -> str:
    return name.replace("\\", "/").lower()


def _unsafe_name(name: str) -> bool:
    """Path traversal or absolute paths. We never extract to disk, but such
    names only appear in crafted archives, so they are a reason to reject."""
    parts = re.split(r"[/\\]", name)
    return (
        name.startswith(("/", "\\"))
        or bool(DRIVE_LETTER.match(name))
        or ".." in parts
        or "\x00" in name
    )
