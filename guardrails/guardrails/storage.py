"""Persistence (SQLite) + tamper-evident audit log.

- Database: one SQLite file, one connection guarded by a lock, explicit
  transactions. A state change and its audit entry commit together or not
  at all.
- SQLiteActionStore: the action queue (same interface as InMemoryActionStore).
- AuditLog: append-only, hash-chained. Each entry stores the hash of the
  previous one, so editing, deleting or reordering any past entry breaks
  the chain and verify_chain() reports where. SQLite triggers also refuse
  UPDATE/DELETE on the table.

Audit entries hold ids, hashes, rule codes and outcomes; never email
bodies, candidate data, approval tokens or secrets.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from .approval import ActionRecord, ApprovalError, Status
from .canonical import canonical_json
from .models import ProposedAction, RecordRef, Violation

GENESIS = "sha256:" + "0" * 64

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions (
    action_id   TEXT PRIMARY KEY,
    agent       TEXT NOT NULL,
    tool        TEXT NOT NULL,
    status      TEXT NOT NULL,
    created_at  REAL NOT NULL,
    action_hash TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_actions_usage ON actions(agent, tool, created_at);

CREATE TABLE IF NOT EXISTS audit_log (
    seq         INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    event       TEXT NOT NULL,
    actor       TEXT NOT NULL,
    action_id   TEXT,
    details     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    entry_hash  TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS ix_audit_action ON audit_log(action_id);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
"""


class Database:
    def __init__(self, path: str | Path = ":memory:"):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self.conn.execute("PRAGMA busy_timeout = 5000")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
            self.conn.execute("PRAGMA synchronous = FULL")
        self.conn.executescript(SCHEMA)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant: nested calls join the outer transaction."""
        with self._lock:
            if self._depth == 0:
                self.conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self.conn
            except BaseException:
                self._depth -= 1
                if self._depth == 0:
                    self.conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if self._depth == 0:
                self.conn.execute("COMMIT")

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            yield self.conn

    def close(self) -> None:
        with self._lock:
            self.conn.close()


# --- action queue ---------------------------------------------------------------

def record_to_json(r: ActionRecord) -> str:
    d = asdict(r)
    d["status"] = r.status.value
    d["action"]["subject"] = {"doctype": r.action.subject.doctype, "name": r.action.subject.name}
    d["action"]["source_hashes"] = list(r.action.source_hashes)
    d["action"]["payload"] = r.action.payload
    d["violations"] = [{"rule": v.rule, "reason": v.reason} for v in r.violations]
    return json.dumps(d, ensure_ascii=False)


def record_from_json(raw: str) -> ActionRecord:
    d = json.loads(raw)
    a = d["action"]
    action = ProposedAction(
        action_id=a["action_id"], agent=a["agent"], tool=a["tool"],
        subject=RecordRef(**a["subject"]), payload=a["payload"],
        source_hashes=tuple(a["source_hashes"]),
        model=a.get("model"), prompt_version=a.get("prompt_version"),
    )
    return ActionRecord(
        action=action,
        action_hash=d["action_hash"],
        status=Status(d["status"]),
        created_at=d["created_at"],
        violations=tuple(Violation(**v) for v in d["violations"]),
        approver=d["approver"],
        approved_at=d["approved_at"],
        approval_token=d["approval_token"],
        rejection_reason=d["rejection_reason"],
        edited_by=list(d["edited_by"]),
        job_criteria=tuple(d["job_criteria"]) if d["job_criteria"] is not None else None,
        executed_at=d["executed_at"],
        result=d["result"],
        error=d["error"],
    )


class SQLiteActionStore:
    """Persistent action queue. get() returns a fresh copy: callers must
    save() their changes, exactly like a real database."""

    def __init__(self, db: Database):
        self.db = db

    def get(self, action_id: str) -> ActionRecord | None:
        with self.db.read() as c:
            row = c.execute("SELECT record_json FROM actions WHERE action_id = ?", (action_id,)).fetchone()
        return record_from_json(row["record_json"]) if row else None

    def add(self, record: ActionRecord) -> None:
        try:
            with self.db.transaction() as c:
                c.execute(
                    "INSERT INTO actions (action_id, agent, tool, status, created_at, action_hash, record_json)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (record.action.action_id, record.action.agent, record.action.tool,
                     record.status.value, record.created_at, record.action_hash, record_to_json(record)),
                )
        except sqlite3.IntegrityError:
            raise ApprovalError("duplicate_action", "action_id already exists") from None

    def save(self, record: ActionRecord) -> None:
        with self.db.transaction() as c:
            cur = c.execute(
                "UPDATE actions SET status = ?, action_hash = ?, record_json = ? WHERE action_id = ?",
                (record.status.value, record.action_hash, record_to_json(record), record.action.action_id),
            )
            if cur.rowcount != 1:
                raise ApprovalError("not_found", "unknown action")

    def claim_for_execution(self, action_id: str) -> bool:
        """Atomic Approved -> Executing, safe across threads AND processes."""
        with self.db.transaction() as c:
            row = c.execute(
                "SELECT record_json FROM actions WHERE action_id = ? AND status = ?",
                (action_id, Status.APPROVED.value),
            ).fetchone()
            if row is None:
                return False
            rec = record_from_json(row["record_json"])
            rec.status = Status.EXECUTING
            cur = c.execute(
                "UPDATE actions SET status = ?, record_json = ? WHERE action_id = ? AND status = ?",
                (Status.EXECUTING.value, record_to_json(rec), action_id, Status.APPROVED.value),
            )
            return cur.rowcount == 1

    def count_recent(self, agent: str, tool: str, since: float, exclude: str | None = None) -> int:
        with self.db.read() as c:
            row = c.execute(
                "SELECT COUNT(*) FROM actions WHERE agent = ? AND tool = ? AND created_at >= ?"
                " AND action_id IS NOT ?",
                (agent, tool, since, exclude),
            ).fetchone()
        return row[0]

    def list(self, status: Status | None = None, limit: int = 100) -> list[ActionRecord]:
        q, args = "SELECT record_json FROM actions", []
        if status is not None:
            q += " WHERE status = ?"
            args.append(status.value)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self.db.read() as c:
            return [record_from_json(r["record_json"]) for r in c.execute(q, args)]


# --- audit log --------------------------------------------------------------------

@dataclass(frozen=True)
class AuditEntry:
    seq: int
    ts: float
    event: str
    actor: str
    action_id: str | None
    details: dict
    prev_hash: str
    entry_hash: str


@dataclass(frozen=True)
class ChainReport:
    ok: bool
    entries: int
    head: str
    broken_at: int | None = None
    reason: str | None = None


def _entry_hash(seq, ts, event, actor, action_id, details, prev_hash) -> str:
    body = {"seq": seq, "ts": ts, "event": event, "actor": actor,
            "action_id": action_id, "details": details, "prev_hash": prev_hash}
    return "sha256:" + hashlib.sha256(canonical_json(body)).hexdigest()


class AuditLog:
    def __init__(self, db: Database, now: Callable[[], float] = time.time):
        self.db = db
        self.now = now

    def append(self, event: str, actor: str, action_id: str | None = None,
               details: Mapping[str, Any] | None = None) -> AuditEntry:
        details = json.loads(canonical_json(details or {}))  # validate + normalise
        with self.db.transaction() as c:
            last = c.execute("SELECT seq, entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
            seq = (last["seq"] + 1) if last else 1
            prev = last["entry_hash"] if last else GENESIS
            ts = round(float(self.now()), 6)
            h = _entry_hash(seq, ts, event, actor, action_id, details, prev)
            c.execute(
                "INSERT INTO audit_log (seq, ts, event, actor, action_id, details, prev_hash, entry_hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (seq, ts, event, actor, action_id, canonical_json(details).decode("utf-8"), prev, h),
            )
        return AuditEntry(seq, ts, event, actor, action_id, details, prev, h)

    def entries(self, action_id: str | None = None) -> list[AuditEntry]:
        return self.query(action_id=action_id)

    def query(self, action_id: str | None = None, after_seq: int | None = None,
              upto_seq: int | None = None, since_ts: float | None = None) -> list[AuditEntry]:
        where, args = [], []
        if action_id is not None:
            where.append("action_id = ?"); args.append(action_id)
        if after_seq is not None:
            where.append("seq > ?"); args.append(after_seq)
        if upto_seq is not None:
            where.append("seq <= ?"); args.append(upto_seq)
        if since_ts is not None:
            where.append("ts > ?"); args.append(since_ts)
        q = "SELECT * FROM audit_log" + (" WHERE " + " AND ".join(where) if where else "")
        with self.db.read() as c:
            rows = c.execute(q + " ORDER BY seq", args).fetchall()
        return [AuditEntry(r["seq"], r["ts"], r["event"], r["actor"], r["action_id"],
                           json.loads(r["details"]), r["prev_hash"], r["entry_hash"]) for r in rows]

    def head(self) -> str:
        """Hash of the latest entry. Publish it somewhere else (README, chat,
        printout) to also detect deletion of the most recent entries."""
        with self.db.read() as c:
            row = c.execute("SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1").fetchone()
        return row["entry_hash"] if row else GENESIS

    def verify_chain(self, expected_head: str | None = None) -> ChainReport:
        prev, n = GENESIS, 0
        for e in self.entries():
            n += 1
            if e.seq != n:
                return ChainReport(False, n, prev, e.seq, "missing or reordered entry")
            if e.prev_hash != prev:
                return ChainReport(False, n, prev, e.seq, "link to previous entry broken")
            if _entry_hash(e.seq, e.ts, e.event, e.actor, e.action_id, e.details, e.prev_hash) != e.entry_hash:
                return ChainReport(False, n, prev, e.seq, "entry content was modified")
            prev = e.entry_hash
        if expected_head is not None and prev != expected_head:
            return ChainReport(False, n, prev, None, "head does not match the published head (entries removed?)")
        return ChainReport(True, n, prev)
