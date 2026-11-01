"""Agent-side audit trail: append-only, hash-chained JSONL under var/ (plus an alerts file for the Monitor).

Each entry stores the SHA-256 of the previous entry, so editing or deleting a line breaks the chain
(`verify()` detects it). The authoritative Audit Log lives in hr_ai (design notes); this file records
what happens inside the agent service: tool calls, rejections, quarantines, budget stops, proposals.
Never log secrets or unmasked PII here.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

from agents.config import get_settings

_lock = threading.Lock()
GENESIS = "0" * 64


def _path(name: str) -> Path:
    p = get_settings().var_dir / name
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _last_hash(path: Path) -> str:
    if not path.is_file() or path.stat().st_size == 0:
        return GENESIS
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 8192))
        last = f.read().splitlines()[-1]
    return json.loads(last)["hash"]


def _append(name: str, event: str, fields: dict) -> dict:
    path = _path(name)
    with _lock:
        prev = _last_hash(path)
        entry = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event, **fields, "prev": prev}
        body = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        entry["hash"] = hashlib.sha256(body.encode()).hexdigest()
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")
    return entry


def log(event: str, **fields) -> dict:
    return _append("audit.jsonl", event, fields)


def alert(kind: str, **fields) -> dict:
    """Monitor alert (channel TODO: open question). Also written to the audit trail."""
    log("alert", kind=kind, **fields)
    return _append("alerts.jsonl", kind, fields)


def verify(name: str = "audit.jsonl") -> tuple[bool, int]:
    """Re-compute the chain. Returns (ok, number_of_entries)."""
    path = _path(name)
    if not path.is_file():
        return True, 0
    prev, n = GENESIS, 0
    for line in path.read_text(encoding="utf-8").splitlines():
        entry = json.loads(line)
        h = entry.pop("hash")
        if entry.get("prev") != prev:
            return False, n
        body = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        if hashlib.sha256(body.encode()).hexdigest() != h:
            return False, n
        prev, n = h, n + 1
    return True, n
