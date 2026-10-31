import io
import secrets
import warnings
import zipfile

import pytest

from ingest.sanitizer.filetype import FileLimits, RejectedFile, check_file

LIMITS = FileLimits(
    max_file_bytes=10_000_000,
    max_zip_entries=1_000,
    max_zip_uncompressed_bytes=50_000_000,
    max_zip_ratio=100,
)

# Fictional minimal files, built in memory.
PDF = b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\ntrailer << /Root 1 0 R >>\n%%EOF\n"
DOCX_PARTS = {
    "[Content_Types].xml": "<Types/>",
    "word/document.xml": "<w:document><w:body><w:p>Fictional CV</w:p></w:body></w:document>",
}


def make_zip(parts) -> bytes:
    """parts: a dict, or a list of (name, content) pairs to allow duplicate names."""
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate-name test writes a name twice
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in parts.items() if isinstance(parts, dict) else parts:
                archive.writestr(name, content)
    return buffer.getvalue()


def mark_encrypted(data: bytes) -> bytes:
    """Set the 'encrypted' flag bit in every central directory entry.

    zipfile cannot write encrypted entries, so the flag is set directly in the
    bytes. In each central directory record ("PK\\x01\\x02"), the flags are at offset 8.
    """
    data = bytearray(data)
    start = data.find(b"PK\x01\x02")
    while start != -1:
        data[start + 8] |= 0x1
        start = data.find(b"PK\x01\x02", start + 4)
    return bytes(data)


def reason_for(data: bytes, limits: FileLimits = LIMITS) -> str:
    with pytest.raises(RejectedFile) as error:
        check_file(data, limits)
    return error.value.reason


# --- Accepted ---

def test_pdf_accepted():
    assert check_file(PDF, LIMITS) == "pdf"


def test_docx_accepted():
    assert check_file(make_zip(DOCX_PARTS), LIMITS) == "docx"


# --- Size and type ---

def test_empty_rejected():
    assert reason_for(b"") == "empty"


def test_too_large_rejected():
    small = FileLimits(1_024, 1_000, 50_000_000, 100)
    assert reason_for(PDF + b"0" * 2_000, small) == "too_large"


@pytest.mark.parametrize("data", [
    b"MZ\x90\x00fictional executable",                  # Windows executable
    b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1old word doc",    # legacy .doc (OLE)
    b"{\\rtf1 fictional}",                              # RTF
    b"<html><body>cv</body></html>",                    # HTML
    b" " + PDF,                                         # junk before %PDF
])
def test_unsupported_type_rejected(data):
    assert reason_for(data) == "unsupported_type"


# --- DOCX zip inspection ---

def test_truncated_zip_rejected():
    assert reason_for(make_zip(DOCX_PARTS)[:40]) == "bad_zip"


def test_macro_rejected():
    parts = {**DOCX_PARTS, "word/vbaProject.bin": b"fake macro"}
    assert reason_for(make_zip(parts)) == "docx_macro"


def test_macro_name_case_insensitive():
    parts = {**DOCX_PARTS, "word/VBAPROJECT.BIN": b"fake macro"}
    assert reason_for(make_zip(parts)) == "docx_macro"


def test_encrypted_entry_rejected():
    assert reason_for(mark_encrypted(make_zip(DOCX_PARTS))) == "zip_encrypted"


@pytest.mark.parametrize("name", ["../evil.xml", "/etc/evil.xml", "C:/evil.xml", "word\\..\\evil.xml"])
def test_bad_path_rejected(name):
    parts = {**DOCX_PARTS, name: "x"}
    assert reason_for(make_zip(parts)) == "zip_bad_path"


def test_duplicate_entry_rejected():
    parts = list(DOCX_PARTS.items()) + [("word/document.xml", "<other/>")]
    assert reason_for(make_zip(parts)) == "zip_duplicate_entry"


def test_too_many_entries_rejected():
    parts = {**DOCX_PARTS, **{f"word/media/f{i}.xml": "x" for i in range(20)}}
    few = FileLimits(10_000_000, 10, 50_000_000, 100)
    assert reason_for(make_zip(parts), few) == "zip_too_many_entries"


def test_zip_bomb_by_ratio_rejected():
    # 5 MB of zeros compresses to a few KB: ratio far above 100.
    parts = {**DOCX_PARTS, "word/media/bomb.bin": b"\x00" * 5_000_000}
    assert reason_for(make_zip(parts)) == "zip_bomb"


def test_zip_bomb_by_total_size_rejected():
    parts = {**DOCX_PARTS, "word/media/big.bin": b"\x00" * 2_000_000}
    tight = FileLimits(10_000_000, 1_000, 1_000_000, 1_000_000)  # ratio check out of the way
    assert reason_for(make_zip(parts), tight) == "zip_bomb"


def test_case_only_duplicate_rejected():
    parts = {**DOCX_PARTS, "Word/Document.xml": "<other/>"}
    assert reason_for(make_zip(parts)) == "zip_duplicate_entry"


def test_nul_byte_in_name_rejected():
    # zipfile cannot write a NUL in a name, so "#" is written, then patched to NUL.
    data = make_zip({**DOCX_PARTS, "word/document.xml#../../evil": "x"})
    assert reason_for(data.replace(b"#", b"\x00")) == "zip_bad_path"


@pytest.mark.parametrize("content_type", [
    "application/vnd.ms-office.vbaProject",                            # renamed macro part
    "application/vnd.ms-word.document.macroEnabled.main+xml",          # .docm main part
])
def test_macro_declared_in_content_types_rejected(content_type):
    types = f'<Types><Override PartName="/word/data.bin" ContentType="{content_type}"/></Types>'
    parts = {**DOCX_PARTS, "[Content_Types].xml": types, "word/data.bin": b"fake"}
    assert reason_for(make_zip(parts)) == "docx_macro"


@pytest.mark.parametrize("name", ["word/activeX/activeX1.xml", "word/embeddings/oleObject1.bin"])
def test_active_content_rejected(name):
    parts = {**DOCX_PARTS, name: b"fake"}
    assert reason_for(make_zip(parts)) == "docx_active_content"


def test_oversized_content_types_rejected():
    # Random-looking text so it does not compress well (no zip_bomb first).
    parts = {**DOCX_PARTS, "[Content_Types].xml": "<Types>" + secrets.token_hex(60_000)}
    assert reason_for(make_zip(parts)) == "docx_bad_content_types"


def test_plain_zip_is_not_docx():
    assert reason_for(make_zip({"notes.txt": "fictional"})) == "not_docx"


def test_reason_never_contains_content():
    error = RejectedFile("docx_macro")
    assert str(error) == "docx_macro"
