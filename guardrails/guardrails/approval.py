"""Human approval, enforced by the backend.

Life of an action:

    submit ──▶ Proposed ──approve──▶ Approved ──(gateway)──▶ Executed | Failed
       │          │  ▲                  │
       │          │  └──── edit ────────┘   (any edit voids the approval)
       │          └──reject──▶ Rejected
       └──▶ Blocked   (policy or fairness said no; cannot be approved)

Approval token = HMAC-SHA256 over {action_id, action_hash, approver, iat,
exp, kid}. It proves that THIS human approved THIS exact content. If one
byte of the action changes, the hash changes and the token is worthless.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .canonical import action_hash, canonical_json
from .fairness import FairnessChecker
from .models import ProposedAction, Violation
from .policy import PolicyEngine

MIN_KEY_BYTES = 32
KEY_ENV = "GUARDRAILS_APPROVAL_KEY"
KEY_ID_ENV = "GUARDRAILS_APPROVAL_KEY_ID"
CLAIM_KEYS = {"v", "kid", "action_id", "action_hash", "approver", "iat", "exp"}


class ApprovalError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class Status(str, Enum):
    PROPOSED = "Proposed"
    BLOCKED = "Blocked"
    APPROVED = "Approved"
    EXECUTING = "Executing"  # claimed by the gateway; never auto-retried
    REJECTED = "Rejected"
    EXECUTED = "Executed"
    FAILED = "Failed"


@dataclass(frozen=True)
class Approver:
    """An authenticated human. Identity and roles come from Frappe auth."""

    user: str
    roles: frozenset[str]


@dataclass
class ActionRecord:
    action: ProposedAction
    action_hash: str
    status: Status
    created_at: float
    violations: tuple[Violation, ...] = ()
    approver: str | None = None
    approved_at: float | None = None
    approval_token: str | None = None
    rejection_reason: str | None = None
    edited_by: list[str] = field(default_factory=list)
    job_criteria: tuple[str, ...] | None = None  # kept so the gateway can re-check fairness
    executed_at: float | None = None
    result: dict | None = None
    error: str | None = None


# --- signing --------------------------------------------------------------------

def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class ApprovalSigner:
    """Issues and verifies approval tokens. `keys` maps key-id -> secret,
    so a key can be rotated: new tokens use `active_kid`, old ones still verify."""

    def __init__(self, keys: Mapping[str, bytes], active_kid: str):
        if active_kid not in keys:
            raise ValueError("active key id not in keyring")
        for kid, key in keys.items():
            if not isinstance(key, bytes) or len(key) < MIN_KEY_BYTES:
                raise ValueError(f"approval key '{kid}' must be >= {MIN_KEY_BYTES} bytes")
        self._keys = dict(keys)
        self.active_kid = active_kid

    @classmethod
    def from_env(cls) -> "ApprovalSigner":
        """Key from .env / secrets manager. Never hard-coded, never in git."""
        raw = os.environ.get(KEY_ENV)
        if not raw:
            raise ValueError(f"{KEY_ENV} is not set; refusing to start")
        kid = os.environ.get(KEY_ID_ENV, "k1")
        return cls({kid: raw.encode("utf-8")}, kid)

    def _mac(self, kid: str, body: str) -> bytes:
        return hmac.new(self._keys[kid], body.encode("ascii"), hashlib.sha256).digest()

    def issue(self, claims: Mapping[str, Any]) -> str:
        claims = {**claims, "kid": self.active_kid, "v": 1}
        body = _b64e(canonical_json(claims))
        return f"{body}.{_b64e(self._mac(self.active_kid, body))}"

    def verify(self, token: str) -> dict:
        """Return the claims if the signature is genuine. Does NOT check
        what the claims say; ApprovalService.verify does that."""
        if not isinstance(token, str) or token.count(".") != 1:
            raise ApprovalError("token_invalid", "malformed token")
        body, sig = token.split(".")
        try:
            claims = json.loads(_b64d(body))
            given = _b64d(sig)
        except (ValueError, UnicodeDecodeError):
            raise ApprovalError("token_invalid", "malformed token") from None
        if not isinstance(claims, dict) or set(claims) != CLAIM_KEYS:
            raise ApprovalError("token_invalid", "unexpected token claims")
        kid = claims["kid"]
        if kid not in self._keys:
            raise ApprovalError("token_invalid", "unknown signing key")
        if not hmac.compare_digest(self._mac(kid, body), given):
            raise ApprovalError("token_invalid", "bad signature (forged or altered token)")
        return claims


# --- storage --------------------------------------------------------------------

class InMemoryActionStore:
    """Development store. Will be replaced by persistent storage in step 5."""

    def __init__(self):
        self._records: dict[str, ActionRecord] = {}
        self._lock = threading.Lock()

    def get(self, action_id: str) -> ActionRecord | None:
        return self._records.get(action_id)

    def add(self, record: ActionRecord) -> None:
        if record.action.action_id in self._records:
            raise ApprovalError("duplicate_action", "action_id already exists")
        self._records[record.action.action_id] = record

    def save(self, record: ActionRecord) -> None:
        self._records[record.action.action_id] = record

    def claim_for_execution(self, action_id: str) -> bool:
        """Atomically move Approved -> Executing. Only one caller can win,
        so two concurrent execute() calls cannot both send the email."""
        with self._lock:
            r = self._records.get(action_id)
            if r is None or r.status is not Status.APPROVED:
                return False
            r.status = Status.EXECUTING
            return True

    def list(self, status: "Status | None" = None, limit: int = 100) -> list[ActionRecord]:
        recs = [r for r in self._records.values() if status is None or r.status is status]
        return sorted(recs, key=lambda r: r.created_at, reverse=True)[:limit]

    def count_recent(self, agent: str, tool: str, since: float, exclude: str | None = None) -> int:
        return sum(
            1 for r in self._records.values()
            if r.action.agent == agent and r.action.tool == tool
            and r.created_at >= since and r.action.action_id != exclude
        )


class _StoreUsage:
    """Adapts the store to the Policy Engine's UsageCounter protocol."""

    def __init__(self, store, now: float, exclude: str | None = None):
        self.store, self.now, self.exclude = store, now, exclude

    def count(self, agent: str, tool: str, window_seconds: int) -> int:
        return self.store.count_recent(agent, tool, self.now - window_seconds, self.exclude)


# --- service --------------------------------------------------------------------

class ApprovalService:
    def __init__(
        self,
        config: Mapping[str, Any],
        policy: PolicyEngine,
        fairness: FairnessChecker,
        signer: ApprovalSigner,
        store=None,
        now: Callable[[], float] = time.time,
        audit=None,
    ):
        self.version = config.get("version")
        self.approver_roles = _role_set(config, "approver_roles")
        self.agent_roles = _role_set(config, "agent_roles")
        if self.approver_roles & self.agent_roles:
            raise ValueError("a role cannot be both an approver role and an agent role")
        ttl = config.get("token_ttl_seconds")
        if not isinstance(ttl, int) or isinstance(ttl, bool) or ttl < 60:
            raise ValueError("token_ttl_seconds must be an integer >= 60")
        unknown = set(config) - {"version", "approver_roles", "agent_roles", "token_ttl_seconds"}
        if unknown or not isinstance(self.version, str):
            raise ValueError(f"bad approval config (unknown keys: {sorted(unknown)})")
        self.ttl = ttl
        self.policy, self.fairness, self.signer = policy, fairness, signer
        self.store = store or InMemoryActionStore()
        self.now = now
        if audit is None:  # every decision is logged, even in tests
            from .storage import AuditLog, Database
            audit = AuditLog(getattr(self.store, "db", None) or Database(":memory:"), now=now)
        self.audit = audit

    def transaction(self):
        """State change + audit entry commit together when they share a DB."""
        db = getattr(self.store, "db", None)
        if db is not None and db is self.audit.db:
            return db.transaction()
        return contextlib.nullcontext()

    def _log_denied(self, op: str, who: "Approver", action_id: str, e: ApprovalError) -> None:
        self.audit.append("approval.denied", who.user, action_id,
                          {"op": op, "code": e.code, "roles": sorted(who.roles)})

    @classmethod
    def from_file(cls, path: str | Path, **kwargs) -> "ApprovalService":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh), **kwargs)

    # -- agent side --

    def submit(self, action: ProposedAction, job_criteria: Iterable[str] | None = None,
               extra_violations: Iterable[Violation] = ()) -> ActionRecord:
        """`extra_violations`: problems found before the policy, e.g. by the
        agent-layer adapter (untrusted token values). Any of them blocks."""
        if self.store.get(action.action_id) is not None:
            raise ApprovalError("duplicate_action", "action_id already exists")
        now = self.now()
        violations = tuple(extra_violations) + self._check(action, job_criteria, _StoreUsage(self.store, now))
        record = ActionRecord(
            action=action,
            action_hash=action_hash(action),
            status=Status.BLOCKED if violations else Status.PROPOSED,
            created_at=now,
            violations=violations,
            job_criteria=tuple(job_criteria) if job_criteria is not None else None,
        )
        with self.transaction():
            self.store.add(record)
            self.audit.append("action.submitted", action.agent, action.action_id, {
                "status": record.status.value,
                "tool": action.tool,
                "subject": f"{action.subject.doctype}/{action.subject.name}",
                "action_hash": record.action_hash,
                "source_hashes": list(action.source_hashes),
                "model": action.model,
                "prompt_version": action.prompt_version,
                "policy_version": self.policy.version,
                "fairness_version": self.fairness.version,
                "violations": [v.rule for v in violations],
            })
        return record

    # -- human side --

    def approve(self, action_id: str, approver: Approver, seen_hash: str) -> str:
        """`seen_hash`: the hash shown to the approver in the UI. If the
        action changed after they looked at it, the approval is refused."""
        try:
            return self._approve(action_id, approver, seen_hash)
        except ApprovalError as e:
            self._log_denied("approve", approver, action_id, e)
            raise

    def reject(self, action_id: str, approver: Approver, reason: str) -> ActionRecord:
        try:
            return self._reject(action_id, approver, reason)
        except ApprovalError as e:
            self._log_denied("reject", approver, action_id, e)
            raise

    def edit(self, action_id: str, new_payload: Mapping[str, Any], editor: Approver,
             job_criteria: Iterable[str] | None = None) -> ActionRecord:
        """A human edits the draft (e.g. rewords an email). The old approval
        is voided and every guardrail runs again on the new content."""
        try:
            return self._edit(action_id, new_payload, editor, job_criteria)
        except ApprovalError as e:
            self._log_denied("edit", editor, action_id, e)
            raise

    def _approve(self, action_id: str, approver: Approver, seen_hash: str) -> str:
        record = self._get(action_id)
        self._require_human_approver(approver, record)
        if record.status is not Status.PROPOSED:
            raise ApprovalError("invalid_state", f"cannot approve an action that is {record.status.value}")
        current = action_hash(record.action)
        if current != record.action_hash:
            raise ApprovalError("integrity_error", "stored action does not match its hash")
        if not hmac.compare_digest(seen_hash, current):
            raise ApprovalError("stale_hash", "the action changed since you viewed it; review it again")

        now = int(self.now())
        token = self.signer.issue({
            "action_id": action_id,
            "action_hash": current,
            "approver": approver.user,
            "iat": now,
            "exp": now + self.ttl,
        })
        record.status = Status.APPROVED
        record.approver = approver.user
        record.approved_at = now
        record.approval_token = token
        with self.transaction():
            self.store.save(record)
            self.audit.append("action.approved", approver.user, action_id, {
                "action_hash": current,
                "token_kid": self.signer.active_kid,
                "token_exp": now + self.ttl,
            })  # the token itself is a credential: never logged
        return token

    def _reject(self, action_id: str, approver: Approver, reason: str) -> ActionRecord:
        record = self._get(action_id)
        self._require_human_approver(approver, record)
        if record.status not in (Status.PROPOSED, Status.APPROVED):
            raise ApprovalError("invalid_state", f"cannot reject an action that is {record.status.value}")
        if not isinstance(reason, str) or not reason.strip():
            raise ApprovalError("reason_required", "a rejection needs a reason")
        record.status = Status.REJECTED
        record.rejection_reason = reason.strip()
        was = record.status.value
        record.approval_token = None
        with self.transaction():
            self.store.save(record)
            self.audit.append("action.rejected", approver.user, action_id, {
                "previous_status": was,
                "action_hash": record.action_hash,
                "reason": record.rejection_reason,
            })
        return record

    def _edit(self, action_id: str, new_payload: Mapping[str, Any], editor: Approver,
              job_criteria: Iterable[str] | None) -> ActionRecord:
        record = self._get(action_id)
        self._require_human_approver(editor, record)
        if record.status not in (Status.PROPOSED, Status.APPROVED, Status.BLOCKED):
            raise ApprovalError("invalid_state", f"cannot edit an action that is {record.status.value}")
        old_hash, was = record.action_hash, record.status.value
        action = replace(record.action, payload=new_payload)
        if job_criteria is not None:
            record.job_criteria = tuple(job_criteria)
        violations = self._check(action, record.job_criteria,
                                 _StoreUsage(self.store, self.now(), exclude=action_id))
        record.action = action
        record.action_hash = action_hash(action)
        record.status = Status.BLOCKED if violations else Status.PROPOSED
        record.violations = violations
        record.approver = record.approved_at = record.approval_token = None
        record.edited_by.append(editor.user)
        with self.transaction():
            self.store.save(record)
            self.audit.append("action.edited", editor.user, action_id, {
                "previous_status": was,
                "old_hash": old_hash,
                "new_hash": record.action_hash,
                "status": record.status.value,
                "approval_voided": was == Status.APPROVED.value,
                "violations": [v.rule for v in violations],
            })
        return record

    # -- gateway side --

    def verify(self, action_id: str, token: str) -> ActionRecord:
        """Called by the Execution Gateway right before executing.
        Raises unless the record is Approved and the token matches the
        CURRENT content, approver and time window."""
        record = self._get(action_id)
        claims = self.signer.verify(token)
        if record.status is not Status.APPROVED:
            raise ApprovalError("invalid_state", f"action is {record.status.value}, not Approved")
        if claims["action_id"] != action_id:
            raise ApprovalError("token_mismatch", "token was issued for another action")
        current = action_hash(record.action)
        if not hmac.compare_digest(claims["action_hash"], current) or current != record.action_hash:
            raise ApprovalError("token_mismatch", "action content changed after approval")
        if claims["approver"] != record.approver:
            raise ApprovalError("token_mismatch", "approver does not match the record")
        if not isinstance(claims["exp"], int) or self.now() >= claims["exp"]:
            raise ApprovalError("token_expired", "approval expired; approve again")
        return record

    def recheck(self, record: ActionRecord) -> tuple[Violation, ...]:
        """Run policy + fairness again (used by the gateway just before
        executing: limits or rules may have changed since approval)."""
        usage = _StoreUsage(self.store, self.now(), exclude=record.action.action_id)
        return self._check(record.action, record.job_criteria, usage)

    # -- internals --

    def _check(self, action, job_criteria, usage) -> tuple[Violation, ...]:
        decision = self.policy.evaluate(action, usage)
        fair = self.fairness.check(action, job_criteria)
        return tuple(decision.violations) + tuple(fair.violations)

    def _get(self, action_id: str) -> ActionRecord:
        record = self.store.get(action_id)
        if record is None:
            raise ApprovalError("not_found", "unknown action")
        return record

    def _require_human_approver(self, who: Approver, record: ActionRecord) -> None:
        if who.roles & self.agent_roles or who.user == record.action.agent \
                or who.user in self.policy.agent_tools:
            raise ApprovalError("self_approval", "AI agents cannot approve, reject or edit actions")
        if not who.roles & self.approver_roles:
            raise ApprovalError("not_authorized", "approver lacks an approver role")


def _role_set(config, key) -> frozenset[str]:
    val = config.get(key)
    if not isinstance(val, list) or not val or not all(isinstance(r, str) and r for r in val):
        raise ValueError(f"{key} must be a non-empty list of role names")
    return frozenset(val)
