"""Run text extraction in an isolated child process and validate what comes back.

Why a child process: PyMuPDF wraps a C library that parses attacker-written
files. A crash, hang or memory blow-up kills only the child; the service gets
a clean RejectedFile instead.

How the child is isolated:
- `python -I`: ignores PYTHON* environment variables, the user site-packages
  and the current folder, so a planted .py file cannot be loaded.
- An almost empty environment: the child never sees HMAC keys or passwords.
- File bytes go through stdin: no temporary file, the filename is never used.
- A timeout kills it; on Linux it also sets its own memory and CPU limits.

The child's output is treated as untrusted too: it is validated against a
strict schema, and only known reason codes are accepted.
"""

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError

from ingest.sanitizer.filetype import FileType, RejectedFile

CHILD_SCRIPT = Path(__file__).with_name("_extract_child.py")
CHILD_REASONS = {
    "too_large", "pdf_encrypted", "too_many_pages", "bad_zip", "zip_bomb",
    "too_much_text", "extract_memory", "extract_failed",
}


@dataclass(frozen=True)
class ExtractLimits:
    max_file_bytes: int
    max_pdf_pages: int
    max_zip_uncompressed_bytes: int
    max_text_chars: int
    timeout_s: int
    memory_mb: int

    @classmethod
    def from_settings(cls, settings) -> "ExtractLimits":
        return cls(
            max_file_bytes=settings.max_file_bytes,
            max_pdf_pages=settings.max_pdf_pages,
            max_zip_uncompressed_bytes=settings.max_zip_uncompressed_bytes,
            max_text_chars=settings.max_text_chars,
            timeout_s=settings.extract_timeout_s,
            memory_mb=settings.extract_memory_mb,
        )


class _Span(BaseModel):
    # allow_inf_nan=False: a NaN size or infinite position would make every
    # "is it hidden?" comparison False, so it is rejected here.
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid", allow_inf_nan=False)


class PdfSpan(_Span):
    """One run of text on a PDF page, with the layout needed to spot hidden text."""

    text: str
    page: int
    line: int
    size: float
    color: int  # sRGB 0xRRGGBB
    alpha: int  # 0 = fully transparent, 255 = opaque
    char_flags: int  # MuPDF bits, incl. whether glyphs are filled / stroked
    bbox: tuple[float, float, float, float]
    page_box: tuple[float, float]


class DocxSpan(_Span):
    """One run of text in a DOCX paragraph, with its formatting."""

    text: str
    part: Literal["body", "header", "footer"]
    paragraph: int
    size: float | None
    color: str | None  # "RRGGBB"
    hidden: bool  # effective value: run, character style, paragraph style


_SPAN_LISTS = {"pdf": TypeAdapter(list[PdfSpan]), "docx": TypeAdapter(list[DocxSpan])}


def _child_env() -> dict[str, str]:
    # Windows needs SYSTEMROOT for Python to start; nothing else is passed.
    if os.name == "nt":
        return {"SYSTEMROOT": os.environ.get("SYSTEMROOT", r"C:\Windows")}
    return {}


def extract(
    data: bytes,
    file_type: FileType,
    limits: ExtractLimits,
    child_script: Path = CHILD_SCRIPT,
) -> list[PdfSpan] | list[DocxSpan]:
    """Return the text spans of a file already accepted by check_file."""
    child_limits = {
        "max_file_bytes": limits.max_file_bytes,
        "max_pdf_pages": limits.max_pdf_pages,
        "max_zip_uncompressed_bytes": limits.max_zip_uncompressed_bytes,
        "max_text_chars": limits.max_text_chars,
        "timeout_s": limits.timeout_s,
        "memory_mb": limits.memory_mb,
    }
    command = [sys.executable, "-I", str(child_script), file_type, json.dumps(child_limits)]
    try:
        completed = subprocess.run(
            command,
            input=data,
            capture_output=True,
            timeout=limits.timeout_s,
            env=_child_env(),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RejectedFile("extract_timeout") from None  # run() has already killed the child

    # stderr is never logged or returned: it could contain file content.
    if completed.returncode != 0:
        raise RejectedFile("extract_failed")
    try:
        # The result is the last line; anything a C library printed before it
        # is ignored (only the last line is decoded).
        last_line = completed.stdout.strip().splitlines()[-1].decode("ascii")
        result = json.loads(last_line)
    except (UnicodeDecodeError, IndexError, ValueError, RecursionError):
        raise RejectedFile("extract_failed") from None
    if not isinstance(result, dict):
        raise RejectedFile("extract_failed")

    if result.get("ok") is not True:
        reason = result.get("reason")
        known = isinstance(reason, str) and reason in CHILD_REASONS
        raise RejectedFile(reason if known else "extract_failed")
    try:
        # Strict JSON-mode validation: no silent type conversion of child output.
        return _SPAN_LISTS[file_type].validate_json(json.dumps(result.get("spans")))
    except ValidationError:
        raise RejectedFile("extract_failed") from None
