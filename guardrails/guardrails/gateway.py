"""Secure Execution Gateway: the ONLY component that writes to Frappe.

execute(action_id, token):
  1. already Executed?  -> return the stored result, write nothing (idempotent)
  2. verify approval    -> status Approved, token genuine, hash = current content
  3. re-check guardrails-> policy + fairness again (limits may have changed)
  4. claim              -> atomic Approved -> Executing (one winner only)
  5. execute            -> tool executor via Frappe REST
  6. record outcome     -> Executed (result) | Failed (error)

Emails are AT MOST ONCE: if Frappe does not answer (timeout), the action
is marked Failed with outcome_unknown and is never retried automatically.
A human checks Frappe and decides. Better a missed email than a double one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .approval import ApprovalError, ApprovalService, Status
from .executors import EXECUTORS, Executor, ExecutionRefused
from .frappe_client import FrappeAPI, FrappeError, FrappeUnavailable


@dataclass(frozen=True)
class ExecutionResult:
    action_id: str
    status: Status
    result: dict | None = None
    error: str | None = None
    already_executed: bool = False


class GatewayError(Exception):
    """Execution refused before anything was written to Frappe."""

    def __init__(self, code: str, message: str, violations: tuple = ()):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.violations = violations


@dataclass
class ExecutionGateway:
    approvals: ApprovalService
    frappe: FrappeAPI
    executors: Mapping[str, Executor] = field(default_factory=lambda: dict(EXECUTORS))

    def execute(self, action_id: str, token: str, actor: str = "gateway") -> ExecutionResult:
        """`actor`: who asked for execution (authenticated user or service)."""
        try:
            return self._execute(action_id, token, actor)
        except GatewayError as e:
            self.approvals.audit.append("execution.refused", actor, action_id, {
                "code": e.code,
                "violations": [v.rule for v in e.violations],
            })
            raise

    def _execute(self, action_id: str, token: str, actor: str) -> ExecutionResult:
        store = self.approvals.store
        record = store.get(action_id)
        if record is None:
            raise GatewayError("not_found", "unknown action")

        # 1. idempotency
        if record.status is Status.EXECUTED:
            self.approvals.audit.append("execution.duplicate", actor, action_id, {})
            return ExecutionResult(action_id, Status.EXECUTED, record.result, already_executed=True)
        if record.status is Status.EXECUTING:
            raise GatewayError("in_progress", "action is already being executed")
        if record.status is Status.FAILED:
            raise GatewayError("failed_needs_review",
                               f"previous attempt failed ({record.error}); a human must review it")

        # 2. approval <-> hash
        try:
            self.approvals.verify(action_id, token)
        except ApprovalError as e:
            raise GatewayError(e.code, e.message) from None

        # 3. guardrails again
        violations = self.approvals.recheck(record)
        if violations:
            raise GatewayError("policy_recheck_failed",
                               "guardrails no longer allow this action", violations)

        executor = self.executors.get(record.action.tool)
        if executor is None:
            raise GatewayError("no_executor", f"tool '{record.action.tool}' cannot be executed")

        # 4. claim (atomic; a concurrent call loses here)
        with self.approvals.transaction():
            if not store.claim_for_execution(action_id):
                raise GatewayError("in_progress", "action was claimed by another execution")
            self.approvals.audit.append("execution.started", actor, action_id, {
                "action_hash": record.action_hash,
                "approver": record.approver,
                "tool": record.action.tool,
            })

        # 5. execute + 6. record
        try:
            result = executor(self.frappe, record.action)
        except ExecutionRefused as e:
            return self._finish(record, actor, Status.FAILED, error=f"refused:{e.code}: {e}")
        except FrappeError as e:
            return self._finish(record, actor, Status.FAILED, error=f"frappe_rejected: {e}")
        except FrappeUnavailable as e:
            return self._finish(record, actor, Status.FAILED, error=f"outcome_unknown: {e}")
        except Exception as e:  # never leave a record stuck in Executing
            return self._finish(record, actor, Status.FAILED, error=f"internal_error: {type(e).__name__}")
        return self._finish(record, actor, Status.EXECUTED, result=result)

    def _finish(self, record, actor, status: Status, result=None, error=None) -> ExecutionResult:
        record.status = status
        record.result = result
        record.error = error
        record.executed_at = self.approvals.now()
        event = "execution.succeeded" if status is Status.EXECUTED else "execution.failed"
        with self.approvals.transaction():
            self.approvals.store.save(record)
            self.approvals.audit.append(event, actor, record.action.action_id, {
                "action_hash": record.action_hash,
                "approver": record.approver,
                "result": result,
                "error": error,
            })
        return ExecutionResult(record.action.action_id, status, result, error)
