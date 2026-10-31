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


PERSON_FIELDS = {
    "Job Applicant": {"names": ["applicant_name"], "emails": ["email_id"]},
    "Employee": {"names": ["employee_name", "first_name", "last_name"],
                 "emails": ["prefered_email", "company_email", "personal_email"]},
}
TOKEN_RE = re.compile(r"\[[A-Z]+_\d+\]")


def person_values(doctype: str, doc: Mapping) -> set[str]:
    """Values a PII token may legitimately stand for: the person's names
    (full and first name) and email addresses, as stored in Frappe."""
    spec = PERSON_FIELDS.get(doctype)
    if spec is None:
        return set()
    out: set[str] = set()
    for f in spec["names"]:
        v = doc.get(f)
        if isinstance(v, str) and v.strip():
            out.add(v.strip().lower())
            out.add(v.strip().split()[0].lower())
    for f in spec["emails"]:
        v = doc.get(f)
        if isinstance(v, str) and v.strip():
            out.add(v.strip().lower())
    return out


def detokenize(text: str, token_map: Mapping[str, str], allowed: set[str]) -> str:
    """Replace [PERSON_1]-style tokens with real values, but only values that
    belong to the recipient's own Frappe record. The token map comes from the
    agent layer and is not trusted on its own."""
    def sub(m):
        token = m.group(0)
        value = token_map.get(token)
        if not isinstance(value, str):
            raise ExecutionRefused("unresolved_token", f"{token} has no value")
        if value.strip().lower() not in allowed:
            raise ExecutionRefused("token_value_not_from_record",
                                   f"{token} does not match the recipient's Frappe record")
        return value
    return TOKEN_RE.sub(sub, text)


def resolve_recipient(frappe: FrappeAPI, ref: RecordRef) -> tuple[str, dict]:
    """Look up the real address in Frappe. This is the ONLY place an address
    enters an outgoing action, so a prompt-injected address cannot."""
    spec = PERSON_FIELDS.get(ref.doctype)
    if spec is None:
        raise ExecutionRefused("recipient_invalid", f"cannot email a {ref.doctype}")
    doc = frappe.get_doc(ref.doctype, ref.name)
    for f in spec["emails"]:
        addr = doc.get(f)
        if isinstance(addr, str) and EMAIL_RE.match(addr.strip()):
            return addr.strip(), doc
    raise ExecutionRefused("recipient_no_email", f"{ref.doctype} {ref.name} has no valid email")


def resolve_email(frappe: FrappeAPI, ref: RecordRef) -> str:
    return resolve_recipient(frappe, ref)[0]


def _send(frappe: FrappeAPI, action: ProposedAction, subject: str, body_text: str) -> dict:
    ref = _single_recipient(action)
    address, doc = resolve_recipient(frappe, ref)
    token_map = action.payload.get("token_map") or {}
    if not isinstance(token_map, Mapping):
        raise ExecutionRefused("payload_invalid", "token_map must be an object")
    allowed = person_values(ref.doctype, doc)
    subject = detokenize(subject, token_map, allowed)
    html_body = _to_html(detokenize(body_text, token_map, allowed))
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

def _allowed_keys(payload: Mapping, required: set[str], optional: set[str]) -> None:
    if not isinstance(payload, Mapping) or not required <= set(payload) <= required | optional:
        got = sorted(payload) if isinstance(payload, Mapping) else type(payload).__name__
        raise ExecutionRefused("payload_invalid",
                               f"expected keys {sorted(required)} (+ optional {sorted(optional)}), got {got}")


def send_email(frappe: FrappeAPI, action: ProposedAction) -> dict:
    p = action.payload
    # token_map / inbound_email come from the agent layer (AI Action format)
    _allowed_keys(p, {"recipients", "subject", "body"}, {"token_map", "inbound_email"})
    return _send(frappe, action, _text(p, "subject", MAX_SUBJECT), _text(p, "body", MAX_BODY))


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
    return _send(frappe, action, _text(p, "subject", MAX_SUBJECT), body)


TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d(:[0-5]\d)?$")


def schedule_interview(frappe: FrappeAPI, action: ProposedAction) -> dict:
    """Agent-layer 'interview' action: create a Frappe HR Interview."""
    p = action.payload
    _allowed_keys(p, {"applicant", "interview_type", "date", "from_time", "to_time"}, {"token_map"})
    ref = action.subject
    if ref.doctype != "Job Applicant":
        raise ExecutionRefused("subject_mismatch", "interviews are scheduled for a Job Applicant")
    token_map = p.get("token_map") or {}
    applicant = token_map.get(p["applicant"], p["applicant"]) if isinstance(token_map, Mapping) else p["applicant"]
    if applicant != ref.name:
        raise ExecutionRefused("subject_mismatch", "payload applicant is not the action subject")
    itype = _text(p, "interview_type", 140)
    try:
        day = datetime.strptime(str(p["date"]), "%Y-%m-%d").date()
    except ValueError:
        raise ExecutionRefused("payload_invalid", "date must be YYYY-MM-DD") from None
    start, end = str(p["from_time"]).strip(), str(p["to_time"]).strip()
    if not TIME_RE.match(start) or not TIME_RE.match(end) or end[:5] <= start[:5]:
        raise ExecutionRefused("payload_invalid", "from_time/to_time must be HH:MM and end after start")
    frappe.get_doc(ref.doctype, ref.name)  # applicant must exist
    doc = frappe.insert_doc("Interview", {
        "interview_type": itype,
        "job_applicant": ref.name,
        "scheduled_on": day.isoformat(),
        "from_time": start,
        "to_time": end,
        "status": "Pending",
    })
    return {"interview": doc.get("name"), "scheduled_on": day.isoformat(), "from": start, "to": end}


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
    "schedule_interview": schedule_interview,
}
