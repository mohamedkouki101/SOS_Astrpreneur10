"""Execution Gateway: the only component that acts on AI Actions, and only on Approved ones.

Flow: HR clicks Approve -> on_update hook enqueues execute(name) -> checks -> handler -> Executed | Failed.
Checks before acting: status is Approved (so a second run is a no-op), the payload still matches
action_hash, and the approval token is valid for (name, action_hash, approver).
Pseudonyms are resolved back to real values ONLY here:
- <PERSON_1>-style tokens from the input security layer: POST /v1/resolve (RESOLVER key held only here),
  scoped to the document (doc_id) the proposal was built from;
- [PERSON_1]-style tokens from the agents' local pipeline: the token_map stored on the AI Action;
- "<SENDER>": the original sender of the inbox email, read from the Inbound Email record (HR-only field).

TEST MODE: every email goes to the address in site config `hr_ai_test_recipient`, never to the
(fictional) applicant. Without that setting, emails are refused.
TODO (security pass): policy.py re-check (default-deny), fairness, hr_ai audit log, monitor alerts.
"""

import html
import json
import re

import frappe
from frappe.utils import now_datetime

from hr_ai.approval import canonical_hash, verify

TOKEN = re.compile(r"\[[A-Z]+_\d+\]")
LAYER_TOKEN = re.compile(r"<[A-Z_]+_[1-9][0-9]{0,3}>")


def on_update(doc, method=None):
    """doc_events hook: queue execution right after a human approval is committed."""
    if doc.status == "Approved" and doc.has_value_changed("status"):
        frappe.enqueue("hr_ai.executor.execute", queue="short", name=doc.name, enqueue_after_commit=True)


def execute(name: str):
    doc = frappe.get_doc("AI Action", name, for_update=True)
    if doc.status != "Approved":
        return doc.status  # idempotent: Executed / Failed / Proposed / Rejected are never (re)run
    try:
        _check(doc)
        handler = HANDLERS.get(doc.kind)
        if not handler:
            # Decision-only kinds (ranking, digest, leave decision...): HR's decision is recorded, the
            # action stays Approved and a human follows up. Nothing is written by the gateway.
            frappe.db.set_value("AI Action", doc.name, "execution_result",
                                "Decision recorded. No automatic step for this kind: HR follows up manually.")
            frappe.db.commit()
            return doc.status
        payload = json.loads(doc.payload)
        tokens = json.loads(doc.token_map or "{}")
        result_doctype, result_name, message = handler(doc, payload, tokens)
        _finish(doc, "Executed", message, result_doctype, result_name)
    except Exception as exc:  # any failure is recorded on the action, never retried silently
        frappe.db.rollback()
        doc = frappe.get_doc("AI Action", name, for_update=True)
        if doc.status == "Approved":
            msg = str(exc) if isinstance(exc, ExecutionError) else f"{type(exc).__name__}: {exc}"
            _finish(doc, "Failed", msg[:500])
        if not isinstance(exc, ExecutionError):
            frappe.log_error(title=f"AI Action {name} failed")
    return doc.status


class ExecutionError(Exception):
    pass


def _check(doc):
    if canonical_hash(doc.kind, doc.subject, doc.payload, doc.source_hashes) != doc.action_hash:
        raise ExecutionError("Integrity check failed: payload does not match the approved action hash")
    if not verify(doc.name, doc.action_hash, doc.approver, doc.approval_token):
        raise ExecutionError("Approval token is missing or invalid")


def _finish(doc, status, message, result_doctype=None, result_name=None):
    doc.status = status
    doc.executed_on = now_datetime()
    doc.execution_result = message
    doc.result_doctype, doc.result_name = result_doctype, result_name
    doc.flags.status_change_allowed = True
    doc.flags.ignore_permissions = True
    doc.save()
    doc.add_comment("Info", f"Execution Gateway: {status}. {message}")
    frappe.db.commit()


def _real(text: str, tokens: dict, doc_id: str | None = None) -> str:
    """Replace pseudonyms with their real values (see module docstring)."""
    text = text or ""
    missing = sorted({t for t in TOKEN.findall(text) if t not in tokens})
    if missing:
        raise ExecutionError(f"No real value recorded for {', '.join(missing)}")
    text = TOKEN.sub(lambda m: tokens[m.group(0)], text)
    layer_tokens = sorted(set(LAYER_TOKEN.findall(text)))
    if layer_tokens:
        if not doc_id:
            raise ExecutionError("Proposal has input-layer tokens but no doc_id to resolve them")
        from hr_ai.ingest import IngestError, resolve_token
        values = {}
        for token in layer_tokens:
            try:
                values[token] = resolve_token(doc_id, token)
            except IngestError as exc:
                raise ExecutionError(f"Token resolver: {exc}") from None
            if values[token] is None:
                raise ExecutionError(f"{token} cannot be resolved (document not released or unknown)")
        text = LAYER_TOKEN.sub(lambda m: values.get(m.group(0), m.group(0)), text)
    return text


# ---------------------------------------------------------------- handlers


def _send_email(doc, p, tokens):
    test_to = frappe.conf.get("hr_ai_test_recipient")
    if not test_to:
        raise ExecutionError("Test mode: set site config hr_ai_test_recipient before sending emails")
    doc_id = p.get("doc_id")
    if p["to"] == "<SENDER>":
        # Reply to an inbox email: the recipient is the original sender, never chosen by the model.
        inbound = frappe.db.get_value("Inbound Email", p.get("email_id"), ["sender_email", "doc_id"], as_dict=True)
        if not inbound or not inbound.sender_email:
            raise ExecutionError("Original email not found: the recipient cannot be determined")
        if doc_id != inbound.doc_id:
            raise ExecutionError("The proposal's doc_id does not match the original email")
        intended = inbound.sender_email
    else:
        intended = _real(p["to"], tokens)
    subject = _real(p["subject"], tokens, doc_id)
    body = _real(p["body"], tokens, doc_id)
    message = (
        f"<p style='color:#888'>[RH Flow test mode] Intended recipient: {html.escape(intended)} "
        f"(AI Action {doc.name})</p>"
        + "".join(f"<p>{html.escape(par).replace(chr(10), '<br>')}</p>" for par in body.split("\n\n"))
    )
    ref = {"reference_doctype": "AI Action", "reference_name": doc.name}
    # Idempotency: an email already handed to SMTP for this action is never sent twice.
    if prior := frappe.get_all("Email Queue", filters={**ref, "status": ["in", ["Sent", "Sending", "Partially Sent"]]},
                               pluck="name", limit=1):
        return "Email Queue", prior[0], f"Email already sent for this action ({prior[0]}); not sent again"
    # v16: sendmail(now=True) only sends after the transaction commits, so queue it and send it here,
    # synchronously, to know the outcome before marking the action Executed.
    q = frappe.sendmail(recipients=[test_to], subject=subject, message=message, **ref)
    if not q:
        raise ExecutionError("Email was not queued (is an outgoing Email Account configured?)")
    q.send()
    status, error = frappe.db.get_value("Email Queue", q.name, ["status", "error"])
    if status != "Sent":
        # Cancel it so the scheduler never retries an email whose action is marked Failed.
        frappe.db.set_value("Email Queue", q.name, "status", "Cancelled")
        frappe.db.commit()
        raise ExecutionError(f"Email {status}: {(error or '').strip()[-300:] or 'see Email Queue'}")
    return "Email Queue", q.name, f"Email sent to {test_to} (test mode; intended for {intended})"


def _create_interview(doc, p, tokens):
    applicant = _real(p["applicant"], tokens)
    if not frappe.db.exists("Job Applicant", applicant):
        raise ExecutionError("Job Applicant not found")
    interview = frappe.get_doc({
        "doctype": "Interview",
        "interview_type": p["interview_type"],
        "job_applicant": applicant,
        "job_opening": frappe.db.get_value("Job Applicant", applicant, "job_title"),
        "scheduled_on": p["date"],
        "from_time": p["from_time"],
        "to_time": p["to_time"],
        "status": "Pending",
    })
    interview.flags.ignore_permissions = True
    interview.insert()
    return "Interview", interview.name, (f"Interview {interview.name} scheduled on {p['date']} "
                                         f"{p['from_time']}-{p['to_time']}")


HANDLERS = {"email_reply": _send_email, "interview": _create_interview}
