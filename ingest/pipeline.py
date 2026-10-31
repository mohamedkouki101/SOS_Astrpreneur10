"""The whole Untrusted Input Zone, end to end.

    bytes -> file check -> extraction -> hidden text -> Unicode/links -> PII -> injection
          -> chunks -> signed envelope -> inspection report -> Evidence Store

Every document gets an inspection report, even a rejected one (the
dashboard shows "Rejected: contains macros"). Only accepted, non-quarantined
documents are released to the AI layer. The pipeline never raises to its
caller: an unexpected error becomes a report with status "error".
"""

import logging
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
    REJECTION_LABELS, REMOVED_LABELS, Hashes, InjectionMatch, InspectionReport, MaskedValue,
    ProtectedValue, Reason, Rejection, RemovedPassage, ReportSource, ReportText, Stage, label_for,
)
from ingest.sanitizer.extract import ExtractLimits, extract
from ingest.sanitizer.filetype import FileLimits, RejectedFile, check_file
from ingest.sanitizer.hidden import FilteredText, filter_spans
from ingest.sanitizer.html_text import html_to_text
from ingest.sanitizer.sanitize import SanitizeResult, finish
from ingest.scanner import ScanResult, scan
from ingest.signing import sign_document
from ingest.store import EvidenceStore

log = logging.getLogger(__name__)
STAGE_ORDER = ["file_check", "extraction", "hidden_text", "unicode", "pii", "injection", "chunking", "signing"]


@dataclass(frozen=True)
class PipelineResult:
    report: InspectionReport
    envelope: dict | None  # signed envelope (JSON), None if rejected
    duplicate: bool  # True if this exact document was already processed


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

    def skip_rest(self, detail: str) -> None:
        done = {stage.stage for stage in self.stages}
        for stage in STAGE_ORDER:
            if stage not in done:
                self.stages.append(Stage(stage=stage, status="skipped", detail=detail))


class Pipeline:
    def __init__(self, settings: Settings, store: EvidenceStore | None = None):
        self.settings = settings
        self.store = store or EvidenceStore(settings.evidence_dir)
        self.file_limits = FileLimits.from_settings(settings)
        self.extract_limits = ExtractLimits.from_settings(settings)
        self._signing_key = settings.ingest_signing_key.get_secret_value().encode("utf-8")
        self._ref_key = settings.ingest_ref_key.get_secret_value().encode("utf-8")

    # --- Entry points ---

    def process_upload(
        self, data: bytes, filename: str | None = None, job_opening_id: str | None = None,
        received_at: str | None = None,
    ) -> PipelineResult:
        """A CV uploaded by a recruiter through Frappe (job opening chosen by the recruiter: trusted)."""
        job = {"id": job_opening_id, "origin": "recruiter_upload"} if job_opening_id else None
        source = {"type": "upload_cv", "message_id": None, "in_reply_to": None, "sender_ref": None}
        return self._run(data, filename, source, job, None, received_at, is_email_body=False)

    def process_email(
        self, body: str, is_html: bool, sender_email: str, message_id: str,
        in_reply_to: str | None = None, job_hint: str | None = None, received_at: str | None = None,
    ) -> PipelineResult:
        """An email body from the HR mailbox (the job hint comes from the email: untrusted)."""
        # doc_id covers the Message-ID too, so two emails with the same text stay separate.
        raw = f"{message_id}\n{sender_email}\n".encode("utf-8") + body.encode("utf-8")
        job = {"id": job_hint, "origin": "email_hint"} if job_hint else None
        source = {"type": "email_body", "message_id": message_id, "in_reply_to": in_reply_to,
                  "sender_ref": email_ref(sender_email, self._ref_key)}
        email = (body, is_html)
        return self._run(raw, None, source, job, sender_email, received_at, is_email_body=True, email=email)

    def process_email_attachment(
        self, data: bytes, filename: str | None, sender_email: str, message_id: str,
        in_reply_to: str | None = None, job_hint: str | None = None, received_at: str | None = None,
    ) -> PipelineResult:
        """A CV attached to an email."""
        job = {"id": job_hint, "origin": "email_hint"} if job_hint else None
        source = {"type": "email_attachment", "message_id": message_id, "in_reply_to": in_reply_to,
                  "sender_ref": email_ref(sender_email, self._ref_key)}
        return self._run(data, filename, source, job, sender_email, received_at, is_email_body=False)

    def get(self, doc_id: str) -> PipelineResult | None:
        report = self.store.get_json("reports", doc_id)
        if report is None:
            return None
        return PipelineResult(InspectionReport.model_validate(report), self.store.get_json("envelopes", doc_id), True)

    # --- The run ---

    def _run(self, raw, filename, source, job, sender_email, received_at, is_email_body, email=None):
        doc_id = sha256_id(raw)
        if self.store.has_report(doc_id):  # same bytes already processed: dedupe
            return self.get(doc_id)

        received_at = received_at or utc_now()
        self.store.put_raw(doc_id, raw)
        self.store.put_json("meta", doc_id, {
            "received_at": received_at, "size_bytes": len(raw),
            "original_filename": filename,  # kept here only, never in the envelope or as a path
            "source_type": source["type"],
        })
        timeline = _Timeline()
        base = dict(doc_id=doc_id, received_at=received_at, job_opening=job, size=len(raw), source_type=source["type"])

        try:
            sanitized = self._sanitize(raw, email, timeline)
        except RejectedFile as rejected:
            report = self._rejected_report(base, rejected.reason, timeline)
            self.store.put_json("reports", doc_id, report.model_dump())
            return PipelineResult(report, None, False)

        try:
            return self._accept(base, source, job, sender_email, sanitized, timeline, is_email_body)
        except Exception as error:  # never crash the demo: report the failure instead
            log.error("pipeline error on %s: %s", doc_id, type(error).__name__)  # no content in logs
            done = {t.stage for t in timeline.stages}
            failed_at = next((s for s in STAGE_ORDER if s not in done), "signing")
            timeline.add(failed_at, "fail", "Internal error")
            report = self._rejected_report(base, "internal_error", timeline, status="error")
            self.store.put_json("reports", doc_id, report.model_dump())
            return PipelineResult(report, None, False)

    def _sanitize(self, raw: bytes, email, timeline: _Timeline) -> SanitizeResult:
        if email is not None:
            body, is_html = email
            if len(body) > self.settings.max_text_chars:
                timeline.add("file_check", "fail", REJECTION_LABELS["too_much_text"])
                raise RejectedFile("too_much_text")
            timeline.add("file_check", "pass", f"Email body ({'HTML' if is_html else 'plain text'})")
            original, visible, removed = html_to_text(body) if is_html else (body, body, [])
            timeline.add("extraction", "pass", "HTML parsed safely, nothing executed" if is_html else "Plain text")
            filtered, file_type = FilteredText(original, visible, removed), "email"
        else:
            try:
                file_type = check_file(raw, self.file_limits)
            except RejectedFile as rejected:
                timeline.add("file_check", "fail", label_for(REJECTION_LABELS, rejected.reason))
                raise
            timeline.add("file_check", "pass", f"Real type: {file_type.upper()} ({max(1, len(raw) // 1024)} KB)")
            try:
                spans = extract(raw, file_type, self.extract_limits)
            except RejectedFile as rejected:
                timeline.add("extraction", "fail", label_for(REJECTION_LABELS, rejected.reason))
                raise
            filtered = filter_spans(spans)
            if filtered.visible_text.strip():
                timeline.add("extraction", "pass", f"{len(spans)} text spans read in an isolated process")
            else:
                timeline.add("extraction", "warn", "No readable text (scanned image?)")

        result = finish(file_type, filtered)
        hidden = [item for item in result.removed if item.why != "html_comment"]
        timeline.add("hidden_text", "warn" if hidden else "pass",
                     f"{len(hidden)} hidden passage(s) removed" if hidden else "Nothing hidden")
        notes = [f"{result.invisible_chars_removed} invisible character(s) removed"]
        if result.links_defanged:
            notes.append(f"{result.links_defanged} link(s) made unclickable")
        timeline.add("unicode", "warn" if result.invisible_chars_removed else "pass", ", ".join(notes))
        return result

    def _accept(self, base, source, job, sender_email, sanitized: SanitizeResult, timeline, is_email_body):
        masked = mask(sanitized.clean_text)
        timeline.add("pii", "pass", f"{len(masked.items)} identifier(s) masked, "
                                    f"{len(masked.removed)} protected attribute(s) removed")

        scanned = scan(masked.masked_text, sanitized.removed, sanitized.invisible_chars_removed, sanitized.signals)
        status = {"clean": "pass", "suspicious": "warn", "quarantined": "fail"}[scanned.verdict]
        timeline.add("injection", status, f"{scanned.verdict.capitalize()} (score {scanned.risk_score_bp})")

        chunks = chunk_text(scanned.neutralized_text, is_email_body) if scanned.verdict != "quarantined" else []
        if scanned.verdict == "quarantined":
            timeline.add("chunking", "skipped", "Quarantined: nothing sent to the AI")
        else:
            timeline.add("chunking", "pass", f"{len(chunks)} chunk(s), each with its SHA-256 id")

        candidate_email = masked.first_email or sender_email
        envelope = IngestedDocument(
            schema_version=SCHEMA_VERSION,
            doc_id=base["doc_id"],
            candidate_ref=email_ref(candidate_email, self._ref_key) if candidate_email else None,
            source=Source(
                type=source["type"], message_id=source["message_id"], in_reply_to=source["in_reply_to"],
                file_ext=None if is_email_body else sanitized.file_type,
                received_at=base["received_at"], sender_ref=source["sender_ref"],
            ),
            job_opening=JobOpening(**job) if job else None,
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

        envelope_json = signed.model_dump(mode="json")
        report = self._accepted_report(base, sanitized, masked, scanned, signed, timeline)
        doc_id = base["doc_id"]
        self.store.put_json("envelopes", doc_id, envelope_json)
        self.store.put_json("pii", doc_id, masked.mapping)
        self.store.put_json("reports", doc_id, report.model_dump())
        return PipelineResult(report, envelope_json, False)

    # --- Reports ---

    @staticmethod
    def _source(base, file_type: str) -> ReportSource:
        return ReportSource(type=base["source_type"], file_type=file_type, size_bytes=base["size"])

    def _accepted_report(self, base, sanitized, masked: MaskResult, scanned: ScanResult, signed, timeline):
        return InspectionReport(
            doc_id=base["doc_id"], received_at=base["received_at"],
            source=self._source(base, sanitized.file_type), job_opening=base["job_opening"],
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
            hashes=Hashes(doc_id=base["doc_id"], chunk_ids=[c.chunk_id for c in signed.chunks],
                          signature=signed.signature),
            released_to_ai=scanned.verdict != "quarantined",
        )

    def _rejected_report(self, base, code: str, timeline: _Timeline, status: str = "rejected"):
        timeline.skip_rest("Stopped: file rejected" if status == "rejected" else "Stopped: processing error")
        return InspectionReport(
            doc_id=base["doc_id"], received_at=base["received_at"],
            source=self._source(base, "unknown"), job_opening=base["job_opening"],
            status=status, rejection=Rejection(code=code, label=label_for(REJECTION_LABELS, code)),
            verdict=None, risk_score_bp=0, language=None, reasons=[], stages=timeline.stages,
            text=ReportText(original="", cleaned="", masked="", sent=""),
            removed=[], masked=[], protected_removed=[], injection_matches=[],
            hashes=Hashes(doc_id=base["doc_id"], chunk_ids=[], signature=None),
            released_to_ai=False,
        )
