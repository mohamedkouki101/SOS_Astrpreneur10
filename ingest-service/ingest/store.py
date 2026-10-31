"""Evidence Store: write-once files, named only by a hash.

    evidence/raw/<doc hex>.bin            original bytes (hash re-checked on every read)
    evidence/meta/<report hex>.json       received_at, size, original filename (kept here only)
    evidence/reports/<report hex>.json    inspection report (HR view, contains PII)
    evidence/envelopes/<report hex>.json  signed envelopes released to the AI layer
    evidence/quarantine/<report hex>.json signed envelopes that must never be sent
    evidence/pii/<report hex>.json        token -> original mapping, kept apart

Security points:
- Paths come only from a validated 64-hex hash: an uploaded filename can
  never become a path (no "../../etc/passwd").
- Write-once and atomic: data goes to a temporary file first, then is linked
  into place only if the name is free. A reader never sees half a file, and
  an existing file is never overwritten (only a failed report can be replaced).
- Every file is created owner-only (0600) from the first byte.
- Raw bytes are hashed again when read: a tampered file is detected.
"""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

AREAS = ("raw", "meta", "reports", "envelopes", "quarantine", "pii")
HEX_ID = re.compile(r"sha256:([0-9a-f]{64})")


class IntegrityError(Exception):
    """Stored bytes no longer match their hash."""


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        for area in AREAS:
            (self.root / area).mkdir(mode=0o700, parents=True, exist_ok=True)

    def _path(self, area: str, item_id: str) -> Path:
        match = HEX_ID.fullmatch(item_id)
        if area not in AREAS or not match:
            raise ValueError("invalid area or id")
        extension = ".bin" if area == "raw" else ".json"
        return self.root / area / (match.group(1) + extension)

    @staticmethod
    def _temp_file(path: Path, data: bytes) -> Path:
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temp, flags, 0o600)  # owner-only from the start
        with os.fdopen(descriptor, "wb") as file:
            file.write(data)
        return temp

    def _write_once(self, path: Path, data: bytes) -> bool:
        if path.exists():
            return False
        temp = self._temp_file(path, data)
        try:
            os.link(temp, path)  # atomic, and fails if the name is already taken
            return True
        except FileExistsError:
            return False
        finally:
            os.unlink(temp)

    @staticmethod
    def _encode(obj) -> bytes:
        return json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")

    # --- Raw bytes ---

    def put_raw(self, doc_id: str, data: bytes) -> bool:
        return self._write_once(self._path("raw", doc_id), data)

    def get_raw(self, doc_id: str) -> bytes:
        data = self._path("raw", doc_id).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != doc_id:
            raise IntegrityError(doc_id)
        return data

    # --- JSON documents ---

    def put_json(self, area: str, item_id: str, obj) -> bool:
        return self._write_once(self._path(area, item_id), self._encode(obj))

    def replace_report(self, report_id: str, obj) -> None:
        """Only for retrying a failed run: atomically swap the old report."""
        path = self._path("reports", report_id)
        os.replace(self._temp_file(path, self._encode(obj)), path)

    def get_json(self, area: str, item_id: str):
        path = self._path(area, item_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def delete(self, area: str, item_id: str) -> None:
        """Undo a partial run (never used on raw evidence)."""
        if area == "raw":
            raise ValueError("raw evidence is never deleted")
        self._path(area, item_id).unlink(missing_ok=True)

    def list_report_ids(self) -> list[str]:
        """Newest first, for the dashboard list."""
        files = sorted((self.root / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return ["sha256:" + path.stem for path in files if re.fullmatch(r"[0-9a-f]{64}", path.stem)]
