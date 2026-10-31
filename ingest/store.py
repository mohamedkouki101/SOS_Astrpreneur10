"""Evidence Store: write-once files, named only by the document hash.

    evidence/raw/<hex>.bin         original bytes (hash re-checked on every read)
    evidence/meta/<hex>.json       received_at, size, original filename (kept here only)
    evidence/reports/<hex>.json    inspection report (HR view, contains PII)
    evidence/envelopes/<hex>.json  signed envelope (masked, for the AI layer)
    evidence/pii/<hex>.json        token -> original mapping, kept apart

Security points:
- Paths come only from a validated 64-hex hash: an uploaded filename can
  never become a path (no "../../etc/passwd").
- Write-once ("x" mode): an existing file is never overwritten.
- Raw bytes are hashed again when read: a tampered file is detected.
"""

import hashlib
import json
import os
import re
from pathlib import Path

AREAS = ("raw", "meta", "reports", "envelopes", "pii")
HEX_ID = re.compile(r"sha256:([0-9a-f]{64})")


class IntegrityError(Exception):
    """Stored bytes no longer match their hash."""


class EvidenceStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        for area in AREAS:
            (self.root / area).mkdir(parents=True, exist_ok=True)

    def _path(self, area: str, doc_id: str) -> Path:
        match = HEX_ID.fullmatch(doc_id)
        if area not in AREAS or not match:
            raise ValueError("invalid area or doc_id")
        extension = ".bin" if area == "raw" else ".json"
        return self.root / area / (match.group(1) + extension)

    @staticmethod
    def _write_once(path: Path, data: bytes) -> bool:
        try:
            with open(path, "xb") as file:  # "x": fails if the file exists
                file.write(data)
        except FileExistsError:
            return False
        return True

    # --- Raw bytes ---

    def put_raw(self, doc_id: str, data: bytes) -> bool:
        return self._write_once(self._path("raw", doc_id), data)

    def get_raw(self, doc_id: str) -> bytes:
        data = self._path("raw", doc_id).read_bytes()
        if "sha256:" + hashlib.sha256(data).hexdigest() != doc_id:
            raise IntegrityError(doc_id)
        return data

    # --- JSON documents ---

    def put_json(self, area: str, doc_id: str, obj) -> bool:
        path = self._path(area, doc_id)
        written = self._write_once(path, json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8"))
        if written and area == "pii":
            os.chmod(path, 0o600)  # owner only (effective on Linux, the Docker target)
        return written

    def get_json(self, area: str, doc_id: str):
        path = self._path(area, doc_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def has_report(self, doc_id: str) -> bool:
        return self._path("reports", doc_id).exists()

    def list_doc_ids(self) -> list[str]:
        """Newest first, for the dashboard list."""
        files = sorted((self.root / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return ["sha256:" + path.stem for path in files if re.fullmatch(r"[0-9a-f]{64}", path.stem)]
