"""Verification of signed calls from RH Flow (hr_ai/webhooks.py), and the idempotency store.

Headers: X-RHFlow-Event-Id, X-RHFlow-Timestamp (unix seconds),
         X-RHFlow-Signature = "sha256=" + HMAC-SHA256(secret, f"{timestamp}.{event_id}.".encode() + body)
- bad or missing signature, or body event_id != header event_id -> rejected (401)
- timestamp outside the replay window                            -> rejected (401)
- same event_id again (a replay or a retry)                      -> never run twice (the stored result is returned)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import threading
import time
from pathlib import Path

EVENT_ID = re.compile(r"^[A-Za-z0-9:._-]{1,120}$")
STALE_RUNNING_SECONDS = 900


class SignatureError(Exception):
    pass


def sign(secret: str, timestamp: str, event_id: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{event_id}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(headers: dict, body: bytes, secret: str, window: int, now: float | None = None) -> dict:
    """Return the parsed event if the request is authentic and fresh; raise SignatureError otherwise."""
    if not secret:
        raise SignatureError("receiver has no WEBHOOK_HMAC_SECRET configured")
    h = {k.lower(): v for k, v in headers.items()}
    event_id, ts, sig = h.get("x-rhflow-event-id", ""), h.get("x-rhflow-timestamp", ""), h.get("x-rhflow-signature", "")
    if not (EVENT_ID.fullmatch(event_id) and ts.isdigit() and sig.startswith("sha256=")):
        raise SignatureError("missing or malformed signature headers")
    if not hmac.compare_digest(sig.encode(), sign(secret, ts, event_id, body).encode()):
        raise SignatureError("bad signature")
    if abs((now if now is not None else time.time()) - int(ts)) > window:
        raise SignatureError("timestamp outside the replay window")
    try:
        event = json.loads(body)
    except ValueError:
        raise SignatureError("body is not JSON") from None
    if not isinstance(event, dict) or event.get("event_id") != event_id:
        raise SignatureError("event id mismatch")
    return event


class EventStore:
    """event_id -> status/result in SQLite (survives restarts). One run per event id."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.execute("CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, type TEXT, status TEXT,"
                         " result TEXT, updated REAL)")
        self._lock = threading.Lock()

    def claim(self, event_id: str, kind: str) -> tuple[bool, dict | None]:
        """(True, None) if this call must run the event; (False, row) if it already ran or is running."""
        with self._lock:
            row = self._db.execute("SELECT status, result, updated FROM events WHERE event_id=?", (event_id,)).fetchone()
            if row:
                status, result, updated = row
                stale = status == "running" and time.time() - updated > STALE_RUNNING_SECONDS
                if status != "failed" and not stale:
                    return False, {"status": status, "result": json.loads(result) if result else None}
            self._db.execute("INSERT OR REPLACE INTO events VALUES (?,?,?,?,?)",
                             (event_id, kind, "running", None, time.time()))
            return True, None

    def finish(self, event_id: str, status: str, result: dict | None = None) -> None:
        with self._lock:
            self._db.execute("UPDATE events SET status=?, result=?, updated=? WHERE event_id=?",
                             (status, json.dumps(result, ensure_ascii=False) if result is not None else None,
                              time.time(), event_id))
