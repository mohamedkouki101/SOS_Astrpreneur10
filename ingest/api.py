"""Internal HTTP API: upload in, inspection reports and envelopes out.

Run:  uvicorn ingest.api:create_app --factory --port 8090
Then: http://localhost:8090/docs  (Swagger: upload a CV live from the browser)

One key per caller, sent in the X-API-Key header:
    UPLOAD_API_KEY    Frappe            POST /v1/upload, POST /v1/email
    REPORT_API_KEY    HR dashboard      GET  /v1/reports, /v1/reports/{id}   (contain PII)
                                        POST /v1/reports/{id}/release, /reject (reason required)
    ENVELOPE_API_KEY  AI layer          GET  /v1/envelopes, /v1/envelopes/{id} (masked only)
So the AI layer can never read a report, and the dashboard can never upload.
"""

import hmac
import re
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, SecretStr, field_validator

from ingest import PIPELINE_VERSION
from ingest.config import Settings, get_settings
from ingest.pii.mask import mask
from ingest.pipeline import DecisionError, Pipeline, PipelineResult
from ingest.report import InspectionReport
from ingest.store import HEX_ID

# Same format as the contract: checked here so a bad value is a clean 422.
FRAPPE_NAME = re.compile(r"[A-Za-z0-9-]{1,64}")
MULTIPART_OVERHEAD = 64 * 1024  # room for form fields and boundaries around the file
UPLOAD_PATHS = {"/v1/upload", "/v1/email"}


class Summary(BaseModel):
    """One line in the dashboard list."""

    report_id: str
    doc_id: str
    received_at: str
    filename: str | None  # for display only, from the Evidence Store metadata
    source_type: str
    file_type: str
    status: Literal["accepted", "rejected", "error"]
    verdict: Literal["clean", "suspicious", "quarantined"] | None
    risk_score_bp: int
    rejection: str | None
    released_to_ai: bool
    decision: Literal["released", "rejected"] | None = None  # set by HR on the dashboard
    duplicate: bool = False


class DecisionIn(BaseModel):
    """Why HR releases or rejects a document: always required, kept in the report."""

    reason: str = Field(max_length=500)

    @field_validator("reason")
    @classmethod
    def not_blank(cls, value: str) -> str:
        value = value.strip()
        if len(value) < 3:
            raise ValueError("a reason of at least 3 characters is required")
        return value


class EmailIn(BaseModel):
    body: str = Field(max_length=1_000_000)
    is_html: bool = False
    sender_email: str = Field(pattern=r"^[^\s@]{1,64}@[^\s@]{1,255}$")
    message_id: str = Field(pattern=r"^[!-~]{1,998}$")
    in_reply_to: str | None = Field(default=None, pattern=r"^[!-~]{1,998}$")
    job_hint: str | None = Field(default=None, pattern=r"^[A-Za-z0-9-]{1,64}$")


def _key_check(expected: SecretStr):
    """A dependency that accepts only this caller's key (constant-time compare)."""
    expected_bytes = expected.get_secret_value().encode("utf-8")

    def check(x_api_key: str | None = Header(default=None)) -> None:
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode("utf-8"), expected_bytes):
            raise HTTPException(status_code=401, detail="unauthorized")

    return check


def _report_id(value: str) -> str:
    """Accept "sha256:<hex>" or the bare hex; anything else is simply not found."""
    value = value if value.startswith("sha256:") else "sha256:" + value
    if not HEX_ID.fullmatch(value):
        raise HTTPException(status_code=404, detail="not found")
    return value


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    pipeline = Pipeline(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        mask("Nom : Warm Up")  # load spaCy now, so the first live upload is fast
        yield

    app = FastAPI(title="Untrusted Input Zone", version=PIPELINE_VERSION, lifespan=lifespan)
    app.state.pipeline = pipeline
    max_request = settings.max_file_bytes + MULTIPART_OVERHEAD

    @app.middleware("http")
    async def guard(request: Request, call_next):
        # Refuse an oversized upload from its declared size, BEFORE the body is
        # read: otherwise the server would first save the whole thing to disk.
        if request.method == "POST" and request.url.path in UPLOAD_PATHS:
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                response = JSONResponse(status_code=411, content={"error": "length required"})
            elif int(length) > max_request:
                response = JSONResponse(status_code=413, content={"error": "too large"})
            else:
                response = None
            if response is not None:
                response.headers["Connection"] = "close"
                return response
        try:
            response = await call_next(request)
        except Exception:
            response = JSONResponse(status_code=500, content={"error": "internal"})  # no details leak
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"  # reports contain PII: never cached
        return response

    # Added last, so it is the outermost layer: even errors get CORS headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[origin.strip() for origin in settings.dashboard_origins.split(",") if origin.strip()],
        allow_methods=["GET", "POST"],
        allow_headers=["X-API-Key", "Content-Type"],
    )

    upload_key = Depends(_key_check(settings.upload_api_key))
    report_key = Depends(_key_check(settings.report_api_key))
    envelope_key = Depends(_key_check(settings.envelope_api_key))

    def summary(report: InspectionReport, duplicate: bool = False) -> Summary:
        meta = pipeline.store.get_json("meta", report.report_id) or {}
        return Summary(
            report_id=report.report_id, doc_id=report.doc_id, received_at=report.received_at,
            filename=meta.get("original_filename"), source_type=report.source.type,
            file_type=report.source.file_type, status=report.status, verdict=report.verdict,
            risk_score_bp=report.risk_score_bp, rejection=report.rejection.label if report.rejection else None,
            released_to_ai=report.released_to_ai,
            decision=report.decision.action if report.decision else None, duplicate=duplicate,
        )

    def result_summary(result: PipelineResult) -> Summary:
        return summary(result.report, result.duplicate)

    # --- Health ---

    @app.get("/health")
    def health():
        return {"status": "ok", "pipeline_version": PIPELINE_VERSION}

    # --- In: Frappe (or Swagger during the demo) ---

    @app.post("/v1/upload", dependencies=[upload_key], response_model=Summary)
    def upload(file: UploadFile = File(...), job_opening_id: str | None = Form(default=None)):
        job_opening_id = job_opening_id or None  # an empty form field means "none"
        if job_opening_id is not None and not FRAPPE_NAME.fullmatch(job_opening_id):
            raise HTTPException(status_code=422, detail="invalid job_opening_id")
        # The guard middleware already refused oversized requests; this exact
        # check catches a file just over the limit.
        data = file.file.read(settings.max_file_bytes + 1)
        if len(data) > settings.max_file_bytes:
            raise HTTPException(status_code=413, detail="file too large")
        return result_summary(pipeline.process_upload(data, file.filename, job_opening_id))

    @app.post("/v1/email", dependencies=[upload_key], response_model=Summary)
    def email(message: EmailIn):
        return result_summary(pipeline.process_email(
            message.body, message.is_html, message.sender_email, message.message_id,
            message.in_reply_to, message.job_hint,
        ))

    # --- Out: HR dashboard ---

    @app.get("/v1/reports", dependencies=[report_key], response_model=list[Summary])
    def list_reports():
        return [summary(report) for report in pipeline.list_reports()]

    @app.get("/v1/reports/{report_id}", dependencies=[report_key], response_model=InspectionReport)
    def get_report(report_id: str):
        result = pipeline.get(_report_id(report_id))
        if result is None:
            raise HTTPException(status_code=404, detail="not found")
        return result.report

    # --- Human decisions: HR on the dashboard (the person decides) ---

    def decide(action, report_id: str, body: DecisionIn) -> Summary:
        try:
            return summary(action(_report_id(report_id), body.reason).report)
        except DecisionError as error:
            status = 404 if error.code == "not_found" else 409
            raise HTTPException(status_code=status, detail=error.code) from None

    @app.post("/v1/reports/{report_id}/release", dependencies=[report_key], response_model=Summary)
    def release(report_id: str, body: DecisionIn):
        """Release a quarantined document to the AI layer, flagged as suspicious."""
        return decide(pipeline.release, report_id, body)

    @app.post("/v1/reports/{report_id}/reject", dependencies=[report_key], response_model=Summary)
    def reject(report_id: str, body: DecisionIn):
        """Reject a document; if it was released, the AI layer can no longer fetch it."""
        return decide(pipeline.reject, report_id, body)

    # --- Out: AI layer (released envelopes only) ---

    @app.get("/v1/envelopes", dependencies=[envelope_key])
    def list_envelopes():
        return [
            {"report_id": r.report_id, "doc_id": r.doc_id, "received_at": r.received_at, "verdict": r.verdict}
            for r in pipeline.list_reports() if r.released_to_ai
        ]

    @app.get("/v1/envelopes/{report_id}", dependencies=[envelope_key])
    def get_envelope(report_id: str):
        result = pipeline.get(_report_id(report_id))
        # Quarantined, rejected or unknown: the same 404, nothing to tell apart.
        if result is None or result.envelope is None:
            raise HTTPException(status_code=404, detail="not found")
        return result.envelope

    return app
