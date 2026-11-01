"""HTTP API of the guardrails service (FastAPI).

Who may call what (identity and roles come from Frappe, see auth.py):

  Agents (agent role + listed in config/api.json agent_accounts)
    POST /actions                  propose an action (as THEIR agent only)
    POST /agent-actions            same, in the agent layer's AI Action format
  Agents or HR
    POST /agent-actions/check      dry run: would the guardrails allow it?
  Ingest service (listed in ingest_accounts)
    POST /input-flags              report a quarantined document
  HR (approver role, never an AI Agent)
    GET  /actions, /actions/{id}   review queue + audit trail of one action
    POST /actions/{id}/approve     needs the hash they reviewed
    POST /actions/{id}/reject      needs a reason
    PUT  /actions/{id}/payload     edit (voids approval, re-runs guardrails)
    POST /actions/{id}/execute     needs the approval token
    GET  /alerts, POST /alerts/{id}/ack
    GET  /audit, /audit/verify
    POST /fairness/disparity
  Anyone authenticated
    GET  /me
  Public
    GET  /health
"""
from __future__ import annotations

import threading
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated, Any, Mapping

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .agent_layer import AgentLayerAdapter, AgentLayerError, AgentOutput
from .approval import ActionRecord, ApprovalError, ApprovalService, Approver, Status
from .auth import AuthError, Authenticator, Identity
from .gateway import ExecutionGateway, GatewayError
from .models import ProposedAction, RecordRef
from .monitor import SEVERITIES, Monitor

HASH = r"^sha256:[0-9a-f]{64}$"
ACTION_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,63}$"

ERROR_STATUS = {
    "not_found": 404,
    "duplicate_action": 409, "invalid_state": 409, "stale_hash": 409,
    "in_progress": 409, "failed_needs_review": 409, "integrity_error": 409,
    "self_approval": 403, "not_authorized": 403, "policy_recheck_failed": 403,
    "no_executor": 403, "token_invalid": 403, "token_mismatch": 403, "token_expired": 403,
    "reason_required": 422,
}


# --- config ---------------------------------------------------------------------

@dataclass(frozen=True)
class ApiConfig:
    agent_accounts: Mapping[str, str]
    ingest_accounts: frozenset[str]
    cors_origins: tuple[str, ...]
    max_body_bytes: int
    max_json_depth: int
    rate_limit_per_minute: int

    @classmethod
    def parse(cls, cfg: Mapping[str, Any]) -> "ApiConfig":
        expected = {"version", "agent_accounts", "ingest_accounts", "cors_origins",
                    "max_body_bytes", "max_json_depth", "rate_limit_per_minute"}
        if set(cfg) != expected:
            raise ValueError(f"api config keys must be exactly {sorted(expected)}")
        if "*" in cfg["cors_origins"]:
            raise ValueError("wildcard CORS origin is not allowed")
        for k in ("max_body_bytes", "max_json_depth", "rate_limit_per_minute"):
            if not isinstance(cfg[k], int) or isinstance(cfg[k], bool) or cfg[k] < 1:
                raise ValueError(f"{k} must be a positive integer")
        return cls(dict(cfg["agent_accounts"]), frozenset(cfg["ingest_accounts"]),
                   tuple(cfg["cors_origins"]), cfg["max_body_bytes"], cfg["max_json_depth"],
                   cfg["rate_limit_per_minute"])


@dataclass
class Services:
    approvals: ApprovalService
    gateway: ExecutionGateway
    monitor: Monitor
    authenticator: Authenticator
    config: ApiConfig
    job_criteria: Mapping[str, tuple[str, ...]]
    agent_layer: AgentLayerAdapter | None = None


# --- request bodies (strict: unknown fields are rejected) -----------------------

class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class SubjectIn(_Strict):
    doctype: str = Field(min_length=1, max_length=140)
    name: str = Field(min_length=1, max_length=140)


class ProposeIn(_Strict):
    action_id: str | None = Field(default=None, pattern=ACTION_ID)
    agent: str = Field(min_length=1, max_length=64)
    tool: str = Field(min_length=1, max_length=64)
    subject: SubjectIn
    payload: dict[str, Any]
    source_hashes: list[Annotated[str, Field(pattern=HASH)]] = Field(default_factory=list, max_length=50)
    model: str | None = Field(default=None, max_length=100)
    prompt_version: str | None = Field(default=None, max_length=100)


class ApproveIn(_Strict):
    seen_hash: str = Field(pattern=HASH)


class RejectIn(_Strict):
    reason: str = Field(min_length=1, max_length=1000)


class EditIn(_Strict):
    payload: dict[str, Any]


class ExecuteIn(_Strict):
    token: str = Field(min_length=1, max_length=4096)


class AckIn(_Strict):
    note: str = Field(default="", max_length=1000)


class InputFlagIn(_Strict):
    source_hash: str = Field(pattern=HASH)
    detector: str = Field(min_length=1, max_length=64)
    rules: list[Annotated[str, Field(max_length=64)]] = Field(min_length=1, max_length=50)
    candidate: str | None = Field(default=None, max_length=140)


class OutcomeIn(_Strict):
    group: str = Field(min_length=1, max_length=64)
    selected: bool


class DisparityIn(_Strict):
    job_opening: str = Field(min_length=1, max_length=140)
    outcomes: list[OutcomeIn] = Field(min_length=1, max_length=10000)


# --- helpers -------------------------------------------------------------------

class RateLimiter:
    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._hits: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and q[0] <= now - 60:
                q.popleft()
            if len(q) >= self.per_minute:
                return False
            q.append(now)
            return True


class BodyLimit:
    """ASGI middleware: refuse bodies over the limit, even when chunked."""

    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers", []):
            if name == b"content-length" and value.isdigit() and int(value) > self.max_bytes:
                return await _too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > self.max_bytes:
                    raise _BodyTooLarge()
            return msg
        try:
            await self.app(scope, limited, send)
        except _BodyTooLarge:
            await _too_large(send)


class _BodyTooLarge(Exception):
    pass


async def _too_large(send):
    await JSONResponse({"error": "payload_too_large"}, status_code=413)(
        {"type": "http"}, None, send)


def json_depth(value: Any, limit: int, depth: int = 0) -> bool:
    if depth > limit:
        return False
    if isinstance(value, dict):
        return all(json_depth(v, limit, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(json_depth(v, limit, depth + 1) for v in value)
    return True


def record_view(r: ActionRecord) -> dict:
    """What HR sees. The approval token is a credential: never returned here."""
    a = r.action
    return {
        "action_id": a.action_id, "agent": a.agent, "tool": a.tool,
        "subject": {"doctype": a.subject.doctype, "name": a.subject.name},
        "payload": a.payload, "source_hashes": list(a.source_hashes),
        "model": a.model, "prompt_version": a.prompt_version,
        "status": r.status.value, "action_hash": r.action_hash,
        "violations": [{"rule": v.rule, "reason": v.reason} for v in r.violations],
        "created_at": r.created_at, "approver": r.approver, "approved_at": r.approved_at,
        "rejection_reason": r.rejection_reason, "edited_by": r.edited_by,
        "executed_at": r.executed_at, "result": r.result, "error": r.error,
    }


def _error(code: str, message: str, status: int | None = None) -> HTTPException:
    return HTTPException(status or ERROR_STATUS.get(code, 400), {"error": code, "message": message})


# --- app ------------------------------------------------------------------------

def create_app(s: Services, enable_docs: bool = True) -> FastAPI:
    app = FastAPI(
        title="HR AI Guardrails & Execution Control",
        version="0.7.0",
        docs_url="/docs" if enable_docs else None,
        redoc_url=None,
        openapi_url="/openapi.json" if enable_docs else None,
    )
    limiter = RateLimiter(s.config.rate_limit_per_minute)
    approvals, audit = s.approvals, s.approvals.audit

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(s.config.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT"],
        allow_headers=["Authorization", "Content-Type", "X-Guardrails-CSRF"],
    )

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response

    app.add_middleware(BodyLimit, max_bytes=s.config.max_body_bytes)

    @app.exception_handler(AuthError)
    async def _auth_error(request: Request, e: AuthError):
        return JSONResponse({"error": e.code, "message": e.message}, status_code=e.status)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, e: Exception):  # never leak internals
        return JSONResponse({"error": "internal_error"}, status_code=500)

    # -- dependencies --

    def identity(request: Request) -> Identity:
        ident = s.authenticator.authenticate(
            request.method, {k.lower(): v for k, v in request.headers.items()}, dict(request.cookies))
        if not limiter.allow(ident.user):
            raise _error("rate_limited", "too many requests, slow down", 429)
        return ident

    def deny(ident: Identity, code: str, message: str, endpoint: str, action_id: str | None = None):
        audit.append("api.denied", ident.user, action_id,
                     {"code": code, "endpoint": endpoint, "roles": sorted(ident.roles)})
        s.monitor.run_once()
        return _error(code, message, 403)

    def require_hr(request: Request, ident: Identity = Depends(identity)) -> Approver:
        if ident.roles & approvals.agent_roles or not ident.roles & approvals.approver_roles:
            raise deny(ident, "forbidden_endpoint", "HR approver role required",
                       f"{request.method} {request.url.path}",
                       request.path_params.get("action_id"))
        return Approver(ident.user, ident.roles)

    def check_depth(payload: Any):
        if not json_depth(payload, s.config.max_json_depth):
            raise _error("payload_too_deep", f"JSON nesting deeper than {s.config.max_json_depth}", 422)

    # -- public --

    @app.get("/health")
    def health():
        return {"status": "ok"}

    @app.get("/me")
    def me(ident: Identity = Depends(identity)):
        caps = []
        if ident.roles & approvals.approver_roles and not ident.roles & approvals.agent_roles:
            caps.append("hr")
        if ident.user in s.config.agent_accounts:
            caps.append(f"agent:{s.config.agent_accounts[ident.user]}")
        if ident.user in s.config.ingest_accounts:
            caps.append("ingest")
        return {"user": ident.user, "roles": sorted(ident.roles), "capabilities": caps}

    # -- agents --

    @app.post("/actions", status_code=201)
    def propose(body: ProposeIn, tasks: BackgroundTasks, ident: Identity = Depends(identity)):
        mapped = s.config.agent_accounts.get(ident.user)
        if mapped is None or not ident.roles & approvals.agent_roles:
            raise deny(ident, "forbidden_endpoint", "only registered agent accounts may propose",
                       "POST /actions", body.action_id)
        if body.agent != mapped:
            raise deny(ident, "agent_impersonation",
                       f"this account is '{mapped}', it cannot propose as '{body.agent}'",
                       "POST /actions", body.action_id)
        check_depth(body.payload)

        action = ProposedAction(
            action_id=body.action_id or f"act-{uuid.uuid4().hex}",
            agent=body.agent, tool=body.tool,
            subject=RecordRef(body.subject.doctype, body.subject.name),
            payload=body.payload, source_hashes=tuple(body.source_hashes),
            model=body.model, prompt_version=body.prompt_version,
        )
        # Job criteria come from our trusted list, never from the agent.
        criteria = None
        if action.tool in approvals.fairness.scored_tools:
            criteria = s.job_criteria.get(str(action.payload.get("job_opening")), ())
        try:
            rec = approvals.submit(action, job_criteria=criteria)
        except ApprovalError as e:
            raise _error(e.code, e.message) from None
        tasks.add_task(s.monitor.run_once)
        return {"action_id": rec.action.action_id, "status": rec.status.value,
                "action_hash": rec.action_hash,
                "violations": [{"rule": v.rule, "reason": v.reason} for v in rec.violations]}

    # -- agent layer (hr_ai "AI Action" format, exactly as the agents produce it) --

    def translate_agent_output(raw: dict, ident: Identity, endpoint: str, hr_allowed: bool):
        if s.agent_layer is None:
            raise _error("not_configured", "agent layer adapter is not configured", 503)
        check_depth(raw)
        try:
            out = AgentOutput.parse(raw)
        except AgentLayerError as e:
            raise _error("agent_output_invalid", str(e), 422) from None
        tr = s.agent_layer.translate(out)
        is_hr = bool(ident.roles & approvals.approver_roles) and not ident.roles & approvals.agent_roles
        mapped = s.config.agent_accounts.get(ident.user)
        if mapped is None or not ident.roles & approvals.agent_roles:
            if not (hr_allowed and is_hr):
                raise deny(ident, "forbidden_endpoint", "only registered agent accounts may submit",
                           endpoint, out.external_id)
        elif tr.action.agent != mapped:
            raise deny(ident, "agent_impersonation",
                       f"this account is '{mapped}', it cannot propose as '{out.agent}'",
                       endpoint, out.external_id)
        criteria = None
        if tr.action.tool in approvals.fairness.scored_tools:
            criteria = s.job_criteria.get(str(tr.action.payload.get("job_opening")), ())
        return out, tr, criteria

    def translated_view(tr) -> dict:
        a = tr.action
        return {"agent": a.agent, "tool": a.tool,
                "subject": {"doctype": a.subject.doctype, "name": a.subject.name},
                "recipients": a.payload.get("recipients")}

    @app.post("/agent-actions", status_code=201)
    def submit_agent_action(raw: dict[str, Any], tasks: BackgroundTasks,
                            ident: Identity = Depends(identity)):
        """Submit a proposal in the agent layer's own format. Stored and queued
        for HR like POST /actions; Blocked if any guardrail objects."""
        out, tr, criteria = translate_agent_output(raw, ident, "POST /agent-actions", hr_allowed=False)
        try:
            rec = approvals.submit(tr.action, job_criteria=criteria, extra_violations=tr.violations)
        except ApprovalError as e:
            raise _error(e.code, e.message) from None
        tasks.add_task(s.monitor.run_once)
        return {"action_id": rec.action.action_id, "status": rec.status.value,
                "action_hash": rec.action_hash, "agent_action_hash": out.action_hash,
                "translated": translated_view(tr),
                "violations": [{"rule": v.rule, "reason": v.reason} for v in rec.violations]}

    @app.post("/agent-actions/check")
    def check_agent_action(raw: dict[str, Any], ident: Identity = Depends(identity)):
        """Dry run: would the guardrails allow this proposal? Nothing is stored.
        For the hr_ai app to call before an AI Action can be approved."""
        out, tr, criteria = translate_agent_output(raw, ident, "POST /agent-actions/check", hr_allowed=True)
        decision = approvals.policy.evaluate(tr.action)
        fair = approvals.fairness.check(tr.action, criteria)
        violations = list(tr.violations) + list(decision.violations) + list(fair.violations)
        return {"external_id": out.external_id, "allowed": not violations,
                "translated": translated_view(tr),
                "violations": [{"rule": v.rule, "reason": v.reason} for v in violations],
                "policy_version": approvals.policy.version,
                "fairness_version": approvals.fairness.version}

    # -- ingest --

    @app.post("/input-flags", status_code=201)
    def input_flag(body: InputFlagIn, tasks: BackgroundTasks, ident: Identity = Depends(identity)):
        if ident.user not in s.config.ingest_accounts:
            raise deny(ident, "forbidden_endpoint", "only the ingest service may report flags",
                       "POST /input-flags")
        entry = s.monitor.report_input_flag(body.source_hash, body.detector, body.rules,
                                            body.candidate, actor=ident.user)
        tasks.add_task(s.monitor.run_once)
        return {"audit_seq": entry.seq}

    # -- HR --

    @app.get("/actions")
    def list_actions(hr: Approver = Depends(require_hr),
                     status: Status | None = None, limit: int = Query(50, ge=1, le=500)):
        return [record_view(r) for r in approvals.store.list(status, limit)]

    @app.get("/actions/{action_id}")
    def get_action(action_id: str, hr: Approver = Depends(require_hr)):
        rec = approvals.store.get(action_id)
        if rec is None:
            raise _error("not_found", "unknown action")
        return {**record_view(rec),
                "audit": [_entry_view(e) for e in audit.entries(action_id)]}

    @app.post("/actions/{action_id}/approve")
    def approve(action_id: str, body: ApproveIn, tasks: BackgroundTasks,
                hr: Approver = Depends(require_hr)):
        try:
            token = approvals.approve(action_id, hr, body.seen_hash)
        except ApprovalError as e:
            tasks.add_task(s.monitor.run_once)
            raise _error(e.code, e.message) from None
        tasks.add_task(s.monitor.run_once)
        rec = approvals.store.get(action_id)
        return {"action_id": action_id, "status": rec.status.value, "approval_token": token,
                "expires_in": approvals.ttl}

    @app.post("/actions/{action_id}/reject")
    def reject(action_id: str, body: RejectIn, tasks: BackgroundTasks,
               hr: Approver = Depends(require_hr)):
        try:
            rec = approvals.reject(action_id, hr, body.reason)
        except ApprovalError as e:
            tasks.add_task(s.monitor.run_once)
            raise _error(e.code, e.message) from None
        tasks.add_task(s.monitor.run_once)
        return record_view(rec)

    @app.put("/actions/{action_id}/payload")
    def edit(action_id: str, body: EditIn, tasks: BackgroundTasks,
             hr: Approver = Depends(require_hr)):
        check_depth(body.payload)
        existing = approvals.store.get(action_id)
        criteria = None
        if existing and existing.action.tool in approvals.fairness.scored_tools:
            criteria = s.job_criteria.get(str(body.payload.get("job_opening")), ())
        try:
            rec = approvals.edit(action_id, body.payload, hr, job_criteria=criteria)
        except ApprovalError as e:
            tasks.add_task(s.monitor.run_once)
            raise _error(e.code, e.message) from None
        tasks.add_task(s.monitor.run_once)
        return record_view(rec)

    @app.post("/actions/{action_id}/execute")
    def execute(action_id: str, body: ExecuteIn, tasks: BackgroundTasks,
                hr: Approver = Depends(require_hr)):
        try:
            res = s.gateway.execute(action_id, body.token, actor=hr.user)
        except GatewayError as e:
            tasks.add_task(s.monitor.run_once)
            raise HTTPException(ERROR_STATUS.get(e.code, 400), {
                "error": e.code, "message": str(e),
                "violations": [{"rule": v.rule, "reason": v.reason} for v in e.violations]}) from None
        tasks.add_task(s.monitor.run_once)
        return {"action_id": action_id, "status": res.status.value, "result": res.result,
                "error": res.error, "already_executed": res.already_executed}

    @app.get("/alerts")
    def alerts(hr: Approver = Depends(require_hr), open_only: bool = True,
               min_severity: str = Query("info", pattern="^(" + "|".join(SEVERITIES) + ")$")):
        s.monitor.run_once()
        return [a.__dict__ | {"evidence": list(a.evidence)}
                for a in s.monitor.alerts(open_only=open_only, min_severity=min_severity)]

    @app.post("/alerts/{alert_id}/ack")
    def ack(alert_id: int, body: AckIn, hr: Approver = Depends(require_hr)):
        try:
            s.monitor.acknowledge(alert_id, hr.user, body.note)
        except ValueError as e:
            raise _error("not_found", str(e), 404) from None
        return {"alert_id": alert_id, "acknowledged_by": hr.user}

    @app.get("/audit")
    def audit_entries(hr: Approver = Depends(require_hr), action_id: str | None = None,
                      after_seq: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)):
        return [_entry_view(e) for e in audit.query(action_id=action_id, after_seq=after_seq)[:limit]]

    @app.get("/audit/verify")
    def audit_verify(hr: Approver = Depends(require_hr),
                     expected_head: str | None = Query(None, pattern=HASH)):
        rep = audit.verify_chain(expected_head)
        return {"ok": rep.ok, "entries": rep.entries, "head": rep.head,
                "broken_at": rep.broken_at, "reason": rep.reason}

    @app.post("/fairness/disparity")
    def disparity(body: DisparityIn, hr: Approver = Depends(require_hr)):
        report, raised = s.monitor.check_disparity(
            body.job_opening, [(o.group, o.selected) for o in body.outcomes], actor=hr.user)
        return {"status": report.status, "threshold": report.threshold,
                "flagged": list(report.flagged),
                "groups": [g.__dict__ for g in report.groups],
                "alerts": [a.id for a in raised]}

    return app


def _entry_view(e) -> dict:
    return {"seq": e.seq, "ts": e.ts, "event": e.event, "actor": e.actor,
            "action_id": e.action_id, "details": e.details,
            "prev_hash": e.prev_hash, "entry_hash": e.entry_hash}
