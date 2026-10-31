"""What each approved tool actually does in Frappe.

Every executor:
  - re-validates its payload strictly (exact keys, types, sizes): defence
    in depth, even though the Output Validator ran upstream,
  - resolves real email addresses from Frappe itself (agents never give one),
  - escapes agent-written text before it becomes HTML.

Frappe field/method names to verify on v16:
  Job Applicant: email_id, job_title (link to Job Opening), applicant_rating (0..1), status
  Employee: prefered_email (sic), company_email, personal_email
  Email: frappe.core.doctype.communication.email.make
"""
from __future__ import annotations

import html
import re
from datetime import datetime
from typing import Any, Callable, Mapping

from .frappe_client import FrappeAPI
from .models import ProposedAction, RecordRef

EMAIL_RE = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[A-Za-z]{2,}$")
APPLICANT_STATUSES = {"Open", "Replied", "Shortlisted", "Rejected", "Hold", "Accepted"}  # Frappe HR v16
MAX_SUBJECT = 200
MAX_BODY = 10_000
MAX_SLOTS = 5


class ExecutionRefused(Exception):
    """The gateway refuses BEFORE touching Frappe. Nothing was written."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


Executor = Callable[[FrappeAPI, ProposedAction], dict]


# --- helpers -----------------------------------------------------------------

def _exact_keys(payload: Mapping, keys: set[str]) -> None:
    if not isinstance(payload, Mapping) or set(payload) != keys:
        got = sorted(payload) if isinstance(payload, Mapping) else type(payload).__name__
        raise ExecutionRefused("payload_invalid", f"expected keys {sorted(keys)}, got {got}")


def _text(payload: Mapping, key: str, max_len: int) -> str:
    val = payload[key]
    if not isinstance(val, str) or not val.strip() or len(val) > max_len:
        raise ExecutionRefused("payload_invalid", f"{key} must be non-empty text <= {max_len} chars")
    return val


def _to_html(text: str) -> str:
    """Agent text is plain text. Escape it so it cannot inject HTML,
    scripts or tracking pixels into the email."""
    return "<br>".join(html.escape(line) for line in text.splitlines())


def _single_recipient(action: ProposedAction) -> RecordRef:
    recipients = action.payload["recipients"]
    if not isinstance(recipients, list) or len(recipients) != 1:
        raise ExecutionRefused("payload_invalid", "exactly one recipient is required")
    ref = RecordRef.parse(recipients[0])
    if ref is None or ref != action.subject:
        raise ExecutionRefused("recipient_invalid", "recipient must be the action subject")
    return ref


def resolve_email(frappe: FrappeAPI, ref: RecordRef) -> str:
    """Look up the real address in Frappe. This is the ONLY place an address
    enters an outgoing action, so a prompt-injected address cannot."""
    doc = frappe.get_doc(ref.doctype, ref.name)
    if ref.doctype == "Job Applicant":
        candidates = [doc.get("email_id")]
    elif ref.doctype == "Employee":
        candidates = [doc.get("prefered_email"), doc.get("company_email"), doc.get("personal_email")]
    else:
        raise ExecutionRefused("recipient_invalid", f"cannot email a {ref.doctype}")
    for addr in candidates:
        if isinstance(addr, str) and EMAIL_RE.match(addr.strip()):
            return addr.strip()
    raise ExecutionRefused("recipient_no_email", f"{ref.doctype} {ref.name} has no valid email")


def _send(frappe: FrappeAPI, action: ProposedAction, subject: str, html_body: str) -> dict:
    ref = _single_recipient(action)
    address = resolve_email(frappe, ref)
    msg = frappe.call("frappe.core.doctype.communication.email.make", {
        "doctype": ref.doctype,
        "name": ref.name,
        "recipients": address,
        "subject": subject,
        "content": html_body,
        "communication_medium": "Email",
        "send_email": 1,
    })
    name = msg.get("name") if isinstance(msg, Mapping) else None
    return {"communication": name, "reference": f"{ref.doctype}/{ref.name}"}


def _parse_dt(value: Any, where: str) -> datetime:
    if not isinstance(value, str):
        raise ExecutionRefused("payload_invalid", f"{where} must be an ISO datetime")
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        raise ExecutionRefused("payload_invalid", f"{where} must be an ISO datetime") from None


# --- executors ------------------------------------------------------------------

def send_email(frappe: FrappeAPI, action: ProposedAction) -> dict:
    p = action.payload
    _exact_keys(p, {"recipients", "subject", "body"})
    return _send(frappe, action, _text(p, "subject", MAX_SUBJECT), _to_html(_text(p, "body", MAX_BODY)))


def answer_employee(frappe: FrappeAPI, action: ProposedAction) -> dict:
    return send_email(frappe, action)


def propose_interview_slots(frappe: FrappeAPI, action: ProposedAction) -> dict:
    p = action.payload
    _exact_keys(p, {"recipients", "subject", "body", "slots"})
    slots = p["slots"]
    if not isinstance(slots, list) or not 1 <= len(slots) <= MAX_SLOTS:
        raise ExecutionRefused("payload_invalid", f"slots must list 1 to {MAX_SLOTS} slots")
    lines = []
    for i, s in enumerate(slots):
        _exact_keys(s, {"start", "end"})
        start, end = _parse_dt(s["start"], f"slots[{i}].start"), _parse_dt(s["end"], f"slots[{i}].end")
        if end <= start:
            raise ExecutionRefused("payload_invalid", f"slots[{i}] ends before it starts")
        lines.append(f"- {start:%A %d %B %Y, %H:%M} to {end:%H:%M}")
    body = _text(p, "body", MAX_BODY) + "\n\n" + "\n".join(lines)
    return _send(frappe, action, _text(p, "subject", MAX_SUBJECT), _to_html(body))


def rank_candidate(frappe: FrappeAPI, action: ProposedAction) -> dict:
    p = action.payload
    _exact_keys(p, {"job_opening", "score", "criteria"})
    ref = action.subject
    applicant = frappe.get_doc(ref.doctype, ref.name)
    if applicant.get("job_title") != p["job_opening"]:
        raise ExecutionRefused("subject_mismatch", "applicant did not apply to this job opening")

    lines = [f"<b>AI screening score: {p['score']}/100</b> for {html.escape(p['job_opening'])}",
             f"<small>action {html.escape(action.action_id)}</small>", "<ul>"]
    for c in p["criteria"]:
        lines.append(f"<li><b>{html.escape(str(c['criterion']))}</b> ({c['score']}): "
                     f"{html.escape(str(c['justification']))}</li>")
    lines.append("</ul>")

    frappe.update_doc(ref.doctype, ref.name, {"applicant_rating": round(p["score"] / 100, 4)})
    comment = frappe.insert_doc("Comment", {
        "comment_type": "Comment",
        "reference_doctype": ref.doctype,
        "reference_name": ref.name,
        "content": "\n".join(lines),
    })
    return {"applicant_rating": round(p["score"] / 100, 4), "comment": comment.get("name")}


def update_applicant_status(frappe: FrappeAPI, action: ProposedAction) -> dict:
    p = action.payload
    _exact_keys(p, {"status", "reason"})
    if p["status"] not in APPLICANT_STATUSES:
        raise ExecutionRefused("payload_invalid", f"status must be one of {sorted(APPLICANT_STATUSES)}")
    reason = _text(p, "reason", 1000)
    ref = action.subject
    frappe.update_doc(ref.doctype, ref.name, {"status": p["status"]})
    frappe.insert_doc("Comment", {
        "comment_type": "Comment",
        "reference_doctype": ref.doctype,
        "reference_name": ref.name,
        "content": f"Status set to <b>{p['status']}</b> (approved AI action "
                   f"{html.escape(action.action_id)}): {html.escape(reason)}",
    })
    return {"status": p["status"]}


# Default-deny: a tool without an executor here can never run.
EXECUTORS: dict[str, Executor] = {
    "send_email": send_email,
    "answer_employee": answer_employee,
    "propose_interview_slots": propose_interview_slots,
    "rank_candidate": rank_candidate,
    "update_applicant_status": update_applicant_status,
}
