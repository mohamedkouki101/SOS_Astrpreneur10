"""The whole Untrusted Input Zone, end to end.

    bytes -> file check -> extraction -> hidden text -> Unicode/links -> PII -> injection
          -> chunks -> signed envelope -> inspection report -> Evidence Store

Rules:
- Every submission gets an inspection report, even a rejected one (the
  dashboard shows "Rejected: contains macros").
- Only accepted, non-quarantined envelopes are returned or stored as
  releasable. Quarantined ones go to a separate area and are never returned.
- The pipeline never raises: any unexpected error becomes a report with
  status "error", which can be retried. Deliberate rejections stay cached.

A submission is identified by report_id = hash(doc_id, source type, job
opening, Message-ID): the same CV uploaded by a recruiter and sent by email
gives two reports, so the recruiter's trusted job choice is never lost.
"""

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from ingest import PIPELINE_VERSION, SCHEMA_VERSION
from ingest.chunker import chunk_text
from ingest.config import Settings
from ingest.contract import Chunk, IngestedDocument, JobOpening, Pii, Scan, Source, sha256_id
from ingest.pii.mask import MaskResult, mask
from ingest.pii.refs import email_ref
from ingest.report import (
    REJECTION_LABELS, REMOVED_LABELS, Decision, Hashes, InjectionMatch, InspectionReport, MaskedValue,
    ProtectedValue, Reason, Rejection, RemovedPassage, ReportSource, ReportText, Stage, label_for,
)
from ingest.sanitizer.extract import ExtractLimits
from ingest.sanitizer.filetype import FileLimits, RejectedFile
from ingest.sanitizer.sanitize import SanitizeResult, sanitize_email_body, sanitize_file
from ingest.scanner import ScanResult, neutralize_text, scan
from ingest.signing import sign_document
from ingest.store import EvidenceStore

log = logging.getLogger(__name__)
STAGE_ORDER = ["file_check", "extraction", "hidden_text", "unicode", "pii", "injection", "chunking", "signing"]
# Failures that may not happen again on a second try: these can be re-processed,
# but only after a pause, so one bad file resent in a loop cannot block everyone.
RETRYABLE = {"extract_timeout", "extract_memory", "internal_error"}
RETRY_AFTER_S = 60
# Files derived from a run. A new run clears leftovers from a crashed one first
# (raw evidence is never touched).
DERIVED_AREAS = ("meta", "envelopes", "quarantine", "pii")


class DecisionError(Exception):
    """A release/reject request that cannot apply: code is "not_found" or "conflict"."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PipelineResult:
    report: InspectionReport
    envelope: dict | None  # signed envelope, ONLY when released to the AI layer
    duplicate: bool  # True if this exact submission was already processed


@dataclass(frozen=True)
class _Submission:
    report_id: str
    doc_id: str
    raw: bytes
    received_at: str
    source_type: str
    message_id: str | None
    in_reply_to: str | None
    sender_email: str | None
    job: dict | None
    filename: str | None
    email: tuple[str, bool] | None  # (body, is_html) for an email body


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Timeline:
    """Records each stage with its duration, for the report's timeline."""

    def __init__(self):
        self.stages: list[Stage] = []
        self._started = time.perf_counter()

    def add(self, stage: str, status: str, detail: str) -> None:
        now = time.perf_counter()
        self.stages.append(Stage(stage=stage, status=status, detail=detail, ms=int((now - self._started) * 1000)))
        self._started = now

    def fail_current(self, detail: str) -> None:
        done = {stage.stage for stage in self.stages}
        current = next((stage for stage in STAGE_ORDER if stage not in done), None)
        if current:
            self.add(current, "fail", detail)

    def skip_rest(self, detail: str) -> None:
        done = {stage.stage for stage in self.stages}
        self.stages += [Stage(stage=s, status="skipped", detail=detail) for s in STAGE_ORDER if s not in done]


class Pipeline:
    def __init__(self, settings: Settings, store: EvidenceStore | None = None):
        self.settings = settings
        self.store = store or EvidenceStore(settings.evidence_dir)
        self.file_limits = FileLimits.from_settings(settings)
        self.extract_limits = ExtractLimits.from_settings(settings)
        self._signing_key = settings.ingest_signing_key.get_secret_value().encode("utf-8")
        self._ref_key = settings.ingest_ref_key.get_secret_value().encode("utf-8")
        # One document at a time: simple, avoids two runs of the same submission
        # racing, and keeps spaCy single-threaded. Plenty for HR volumes.
        self._processing = threading.Lock()
        self.retry_after_s = RETRY_AFTER_S

    # --- Entry points ---

    def process_upload(self, data: bytes, filename: str | None = None, job_opening_id: str | None = None,
                       received_at: str | None = None) -> PipelineResult:
        """A CV uploaded by a recruiter through Frappe (job opening chosen by the recruiter: trusted)."""
        job = {"id": job_opening_id, "origin": "recruiter_upload"} if job_opening_id else None
        return self._run(raw=data, source_type="upload_cv", job=job, filename=filename, received_at=received_at)

    def process_email(self, body: str, is_html: bool, sender_email: str, message_id: str,
                      in_reply_to: str | None = None, job_hint: str | None = None,
                      received_at: str | None = None) -> PipelineResult:
        """An email body from the HR mailbox (the job hint comes from the email: untrusted).

        Note for the AI layer: this entry point receives the decoded body, not the
        raw RFC 822 message, so doc_id = sha256(Message-ID + sender + body). Two
        emails with the same text from different people stay separate.
        """
        raw = f"{message_id}\n{sender_email}\n".encode("utf-8") + body.encode("utf-8")
        job = {"id": job_hint, "origin": "email_hint"} if job_hint else None
        return self._run(raw=raw, source_type="email_body", message_id=message_id, in_reply_to=in_reply_to,
                         sender_email=sender_email, job=job, email=(body, is_html), received_at=received_at)

    def process_email_attachment(self, data: bytes, filename: str | None, sender_email: str, message_id: str,
                                 in_reply_to: str | None = None, job_hint: str | None = None,
                                 received_at: str | None = None) -> PipelineResult:
        """A CV attached to an email."""
        job = {"id": job_hint, "origin": "email_hint"} if job_hint else None
        return self._run(raw=data, source_type="email_attachment", message_id=message_id, in_reply_to=in_reply_to,
                         sender_email=sender_email, job=job, filename=filename, received_at=received_at)

    def get(self, report_id: str) -> PipelineResult | None:
        stored = self.store.get_json("reports", report_id)
        if stored is None:
            return None
        report = InspectionReport.model_validate(stored)
        envelope = self.store.get_json("envelopes", report_id) if report.released_to_ai else None
        return PipelineResult(report, envelope, True)

    def list_reports(self) -> list[InspectionReport]:
        return [InspectionReport.model_validate(self.store.get_json("reports", report_id))
                for report_id in self.store.list_report_ids()]

    # --- Human decisions (HR dashboard) ---

    def _decidable(self, report_id: str) -> InspectionReport:
        result = self.get(report_id)
        if result is None:
            raise DecisionError("not_found")
        if result.report.status != "accepted" or result.report.decision is not None:
            raise DecisionError("conflict")  # rejected files, errors, or already decided
        return result.report

    def release(self, report_id: str, reason: str) -> PipelineResult:
        """HR releases a quarantined document after reading it.

        The quarantined envelope has no chunks (the contract forbids them), so a
        new one is built from the masked text, with every rule match neutralized.
        It is marked "suspicious" + "human_released", so the AI layer still
        treats it as flagged data, then signed and moved to envelopes/."""
        with self._processing:
            report = self._decidable(report_id)
            quarantined = self.store.get_json("quarantine", report_id)
            if report.verdict != "quarantined" or quarantined is None:
                raise DecisionError("conflict")

            text = neutralize_text(report.text.masked)
            chunks = chunk_text(text, report.source.type == "email_body")
            envelope = dict(quarantined, signature=None)
            envelope["scan"] = dict(quarantined["scan"], verdict="suspicious",
                                    signals=(quarantined["scan"]["signals"] + ["human_released"])[:100])
            envelope["chunks"] = [{"chunk_id": sha256_id(t.encode("utf-8")), "section": s, "text": t}
                                  for s, t in chunks]
            signed = sign_document(IngestedDocument.model_validate(envelope), self._signing_key)
            envelope_json = signed.model_dump(mode="json")

            updated = report.model_copy(update={
                "released_to_ai": True,
                "decision": Decision(action="released", reason=reason, decided_at=utc_now()),
                "text": report.text.model_copy(update={"sent": "\n\n".join(c.text for c in signed.chunks)}),
                "hashes": Hashes(doc_id=report.doc_id, chunk_ids=[c.chunk_id for c in signed.chunks],
                                 signature=signed.signature),
            })
            self._move_envelope(report_id, envelope_json, "quarantine", "envelopes", updated)
            return PipelineResult(updated, envelope_json, False)

    def reject(self, report_id: str, reason: str) -> PipelineResult:
        """HR rejects a document. If it had been released, its envelope is
        moved back to quarantine/, so the AI layer can no longer fetch it."""
        with self._processing:
            report = self._decidable(report_id)
            updated = report.model_copy(update={
                "released_to_ai": False,
                "decision": Decision(action="rejected", reason=reason, decided_at=utc_now()),
            })
            envelope = self.store.get_json("envelopes", report_id)
            if envelope is not None:
                self._move_envelope(report_id, envelope, "envelopes", "quarantine", updated)
            else:
                self.store.replace_report(report_id, updated.model_dump())
            return PipelineResult(updated, None, False)

    def _move_envelope(self, report_id, envelope, source: str, target: str, report: InspectionReport):
        """Write the new location, then the report, then remove the old one;
        undo the write if the report cannot be saved."""
        if not self.store.put_json(target, report_id, envelope):
            raise DecisionError("conflict")
        try:
            self.store.replace_report(report_id, report.model_dump())
        except Exception:
            self.store.delete(target, report_id)
            raise
        self.store.delete(source, report_id)

    # --- The run ---

    def _run(self, *, raw: bytes, source_type: str, message_id: str | None = None, in_reply_to: str | None = None,
             sender_email: str | None = None, job: dict | None = None, filename: str | None = None,
             email: tuple[str, bool] | None = None, received_at: str | None = None) -> PipelineResult:
        doc_id = sha256_id(raw)
        key = f"{doc_id}|{source_type}|{(job or {}).get('id') or ''}|{message_id or ''}"
        sub = _Submission(
            report_id=sha256_id(key.encode("utf-8")), doc_id=doc_id, raw=raw,
            received_at=received_at or utc_now(), source_type=source_type, message_id=message_id,
            in_reply_to=in_reply_to, sender_email=sender_email, job=job, filename=filename, email=email,
        )
        timeline = _Timeline()
        with self._processing:  # the error path below runs inside the lock too
            try:
                return self._run_locked(sub, timeline)
            except Exception as error:  # never crash the caller (or the demo)
                log.error("pipeline error on %s: %s", sub.report_id, type(error).__name__)  # no content in logs
                timeline.fail_current("Internal error")
                report = self._failed_report(sub, "internal_error", timeline, status="error")
                try:
                    self._save_report(sub.report_id, report, replace=True)
                except Exception:
                    log.error("could not store error report for %s", sub.report_id)
                return PipelineResult(report, None, False)

    def _can_retry(self, report: InspectionReport) -> bool:
        if report.rejection is None or report.rejection.code not in RETRYABLE:
            return False
        failed_at = datetime.strptime(report.received_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - failed_at).total_seconds() >= self.retry_after_s

    def _run_locked(self, sub: _Submission, timeline: _Timeline) -> PipelineResult:
        existing = self.get(sub.report_id)
        retry = existing is not None
        if existing is not None and not self._can_retry(existing.report):
            return existing  # already processed (or failed too recently): dedupe

        # No valid report exists, so anything derived that is still on disk is a
        # leftover from a crashed run: clear it so old and new never get mixed.
        for area in DERIVED_AREAS:
            self.store.delete(area, sub.report_id)
        self.store.put_raw(sub.doc_id, sub.raw)
        self.store.put_json("meta", sub.report_id, {
            "doc_id": sub.doc_id, "received_at": sub.received_at, "size_bytes": len(sub.raw),
            "original_filename": sub.filename,  # kept here only, never in the envelope or as a path
            "source_type": sub.source_type,
        })

        try:
            if sub.email is not None:
                body, is_html = sub.email
                sanitized = sanitize_email_body(body, is_html, self.settings.max_text_chars, timeline.add)
            else:
                sanitized = sanitize_file(sub.raw, self.file_limits, self.extract_limits, timeline.add)
        except RejectedFile as rejected:
            report = self._failed_report(sub, rejected.reason, timeline)
            self._save_report(sub.report_id, report, replace=retry)
            return PipelineResult(report, None, False)

        report, envelope, mapping = self._accept(sub, sanitized, timeline)
        self._save_all(sub.report_id, report, envelope, mapping, replace=retry)
        return PipelineResult(report, envelope if report.released_to_ai else None, False)

    def _accept(self, sub: _Submission, sanitized: SanitizeResult, timeline: _Timeline):
        masked = mask(sanitized.clean_text)
        timeline.add("pii", "pass", f"{len(masked.items)} identifier(s) masked, "
                                    f"{len(masked.removed)} protected attribute(s) removed")

        scanned = scan(masked.masked_text, sanitized.removed, sanitized.invisible_chars_removed, sanitized.signals)
        status = {"clean": "pass", "suspicious": "warn", "quarantined": "fail"}[scanned.verdict]
        timeline.add("injection", status, f"{scanned.verdict.capitalize()} (score {scanned.risk_score_bp})")

        is_email_body = sub.source_type == "email_body"
        if scanned.verdict == "quarantined":
            chunks = []
            timeline.add("chunking", "skipped", "Quarantined: nothing sent to the AI")
        else:
            chunks = chunk_text(scanned.neutralized_text, is_email_body)
            timeline.add("chunking", "pass", f"{len(chunks)} chunk(s), each with its SHA-256 id")

        sender_ref = email_ref(sub.sender_email, self._ref_key) if sub.sender_email else None
        # The sender's address (checked by the mail server) wins over an address
        # written in the document, which the author fully controls.
        candidate_email = sub.sender_email or masked.first_email
        envelope = IngestedDocument(
            schema_version=SCHEMA_VERSION,
            doc_id=sub.doc_id,
            candidate_ref=email_ref(candidate_email, self._ref_key) if candidate_email else None,
            source=Source(
                type=sub.source_type, message_id=sub.message_id, in_reply_to=sub.in_reply_to,
                file_ext=None if is_email_body else sanitized.file_type,
                received_at=sub.received_at, sender_ref=sender_ref,
            ),
            job_opening=JobOpening(**sub.job) if sub.job else None,
            language=masked.language,
            trust="untrusted",
            scan=Scan(verdict=scanned.verdict, risk_score_bp=scanned.risk_score_bp, signals=scanned.signals),
            pii=Pii(masked=True, token_scope="doc", tokens=masked.tokens, protected_removed=masked.protected_removed),
            chunks=[Chunk(chunk_id=sha256_id(text.encode("utf-8")), section=section, text=text)
                    for section, text in chunks],
            pipeline_version=PIPELINE_VERSION,
        )
        signed = sign_document(envelope, self._signing_key)
        timeline.add("signing", "pass", "Envelope signed (HMAC-SHA256)")
        report = self._accepted_report(sub, sanitized, masked, scanned, signed, timeline)
        return report, signed.model_dump(mode="json"), masked.mapping

    # --- Storage: report last, and undo partial writes on failure ---

    def _save_report(self, report_id: str, report: InspectionReport, replace: bool) -> None:
        if replace:
            self.store.replace_report(report_id, report.model_dump())
        else:
            self.store.put_json("reports", report_id, report.model_dump())

    def _save_all(self, report_id, report, envelope, mapping, replace: bool) -> None:
        area = "envelopes" if report.released_to_ai else "quarantine"
        written = []
        try:
            for target, obj in ((area, envelope), ("pii", mapping)):
                if self.store.put_json(target, report_id, obj):
                    written.append(target)
            self._save_report(report_id, report, replace)
        except Exception:
            for target in written:  # never leave an envelope without its report
                self.store.delete(target, report_id)
            raise

    # --- Reports ---

    @staticmethod
    def _common(sub: _Submission, file_type: str) -> dict:
        return dict(
            report_id=sub.report_id, doc_id=sub.doc_id, received_at=sub.received_at,
            source=ReportSource(type=sub.source_type, file_type=file_type, size_bytes=len(sub.raw)),
            job_opening=sub.job,
        )

    def _accepted_report(self, sub, sanitized, masked: MaskResult, scanned: ScanResult, signed, timeline):
        return InspectionReport(
            **self._common(sub, sanitized.file_type),
            status="accepted", rejection=None,
            verdict=scanned.verdict, risk_score_bp=scanned.risk_score_bp, language=masked.language,
            reasons=[Reason(code=r.code, label=r.label, points=r.points) for r in scanned.reasons],
            stages=timeline.stages,
            text=ReportText(
                original=sanitized.original_text, cleaned=sanitized.clean_text, masked=masked.masked_text,
                sent="\n\n".join(chunk.text for chunk in signed.chunks),
            ),
            removed=[RemovedPassage(why=i.why, label=label_for(REMOVED_LABELS, i.why), text=i.text, location=i.location)
                     for i in sanitized.removed],
            masked=[MaskedValue(token=i.token, entity=i.entity, original=i.original, count=i.count)
                    for i in masked.items],
            protected_removed=[ProtectedValue(attribute=r.attribute, original=r.original) for r in masked.removed],
            injection_matches=[InjectionMatch(rule=m.rule_id, label=m.label, text=m.text, where=m.where)
                               for m in scanned.matches],
            hashes=Hashes(doc_id=sub.doc_id, chunk_ids=[c.chunk_id for c in signed.chunks],
                          signature=signed.signature),
            released_to_ai=scanned.verdict != "quarantined",
        )

    def _failed_report(self, sub, code: str, timeline: _Timeline, status: str = "rejected"):
        timeline.skip_rest("Stopped: file rejected" if status == "rejected" else "Stopped: processing error")
        return InspectionReport(
            **self._common(sub, "unknown"),
            status=status, rejection=Rejection(code=code, label=label_for(REJECTION_LABELS, code)),
            verdict=None, risk_score_bp=0, language=None, reasons=[], stages=timeline.stages,
            text=ReportText(original="", cleaned="", masked="", sent=""),
            removed=[], masked=[], protected_removed=[], injection_matches=[],
            hashes=Hashes(doc_id=sub.doc_id, chunk_ids=[], signature=None),
            released_to_ai=False,
        )
