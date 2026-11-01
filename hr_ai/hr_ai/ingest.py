"""Bridge between HR Flow and the input security layer (Untrusted Input Zone, `input-security-layer/`).

Every document that comes from outside passes through the layer BEFORE anything else reads it:

  CV attached to a Job Applicant -> POST /v1/upload (UPLOAD key)
        -> verdict, report_id and doc_id written on the applicant (custom fields)
  Mail to the HR inbox (Mailpit) -> POST /v1/email, CVs attached -> POST /v1/email/attachment (UPLOAD key)
        -> one Inbound Email record (verdict, report ids; sender and subject at permlevel 1)
  HR "Security" tab              -> /v1/reports, /release, /reject (REPORT key, HR Manager only, server side)
  Execution Gateway              -> /v1/resolve (RESOLVER key, approved actions only)

Agents never call these endpoints and never get these keys: they read the Inbound Email / Job
Applicant ids through REST and fetch released, masked, signed envelopes with their own ENVELOPE key.

Site config (bench set-config, never in git): hr_ai_ingest_url, hr_ai_ingest_upload_key,
hr_ai_ingest_report_key, hr_ai_ingest_resolver_key, hr_ai_mailpit_url, hr_ai_hr_inbox.
"""

import email
import html
import json
import re
from email.policy import default

import frappe
import requests
from frappe.utils import now_datetime

FRAPPE_NAME = re.compile(r"^[A-Za-z0-9-]{1,64}$")
MESSAGE_ID = re.compile(r"^[!-~]{1,998}$")
SENDER = re.compile(r"^[^\s@]{1,64}@[^\s@]{1,255}$")
REPORT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")
CV_TYPES = {"application/pdf": "pdf",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx"}
DECIDERS = {"HR Manager", "System Manager"}


class IngestError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- HTTP


def _conf(key: str) -> str:
    value = frappe.conf.get(key)
    if not value:
        raise IngestError(f"site config '{key}' is not set")
    return value


def _call(method: str, path: str, key_name: str, timeout: int = 120, **kwargs):
    url = _conf("hr_ai_ingest_url").rstrip("/") + path
    try:
        r = requests.request(method, url, headers={"X-API-Key": _conf(key_name)}, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise IngestError(f"input security layer unreachable ({type(exc).__name__})") from None
    if r.status_code >= 400:  # never echo the response body: it may contain document text
        raise IngestError(f"input security layer {path.split('/')[2] if path.count('/') > 1 else path}: "
                          f"HTTP {r.status_code}", r.status_code)
    return r.json()


def verdict_of(summary: dict) -> str:
    """One word for HR Flow: clean / suspicious / quarantined / released / rejected / error."""
    if summary.get("decision") in ("released", "rejected"):
        return summary["decision"]
    if summary.get("status") == "rejected":
        return "rejected"
    if summary.get("status") != "accepted":
        return "error"
    return summary.get("verdict") or "error"


# ---------------------------------------------------------------- CVs (Job Applicant)


def on_job_applicant_update(doc, method=None):
    """doc_events: a new or changed CV is sent to the layer before any agent can read it."""
    if doc.resume_attachment and (doc.has_value_changed("resume_attachment") or not doc.get("rhflow_ingest_report_id")):
        frappe.enqueue("hr_ai.ingest.forward_cv", queue="short", job_applicant=doc.name, enqueue_after_commit=True)


def forward_cv(job_applicant: str) -> dict | None:
    app = frappe.db.get_value("Job Applicant", job_applicant, ["resume_attachment", "job_title"], as_dict=True)
    if not app or not app.resume_attachment:
        return None
    file_name = (frappe.db.get_value("File", {"file_url": app.resume_attachment, "attached_to_doctype": "Job Applicant",
                                              "attached_to_name": job_applicant}, "name")
                 or frappe.db.get_value("File", {"file_url": app.resume_attachment}, "name"))
    if not file_name:
        raise IngestError("CV file not found")
    file = frappe.get_doc("File", file_name)
    data = file.get_content()
    data = data.encode() if isinstance(data, str) else data
    form = {"job_opening_id": app.job_title} if app.job_title and FRAPPE_NAME.match(app.job_title) else {}
    summary = _call("POST", "/v1/upload", "hr_ai_ingest_upload_key",
                    files={"file": (file.file_name or "cv", data)}, data=form)
    frappe.db.set_value("Job Applicant", job_applicant, {
        "rhflow_ingest_report_id": summary["report_id"], "rhflow_ingest_doc_id": summary["doc_id"],
        "rhflow_ingest_verdict": verdict_of(summary)}, update_modified=False)
    return summary


def backfill_cvs(limit: int = 500) -> dict:
    """Send every applicant CV not yet inspected (e.g. the seeded demo data) through the layer."""
    names = frappe.get_all("Job Applicant", filters={"resume_attachment": ["is", "set"],
                                                     "rhflow_ingest_report_id": ["is", "not set"]},
                           pluck="name", limit=limit)
    verdicts: dict[str, int] = {}
    for name in names:
        try:
            v = verdict_of(forward_cv(name) or {})
        except IngestError:
            frappe.log_error(title="Input security layer: CV not forwarded")
            v = "error"
        verdicts[v] = verdicts.get(v, 0) + 1
        frappe.db.commit()
    return {"processed": len(names), "verdicts": verdicts}


# ---------------------------------------------------------------- HR inbox (Mailpit)


def sync_inbox_job():
    """Scheduler entry: never raises (the layer or Mailpit may be down)."""
    try:
        sync_inbox()
    except IngestError:
        frappe.log_error(title="Input security layer: inbox sync failed")
    from hr_ai.webhooks import retry_failed_events
    retry_failed_events()


def sync_inbox(limit: int = 100) -> dict:
    base = _conf("hr_ai_mailpit_url").rstrip("/")
    inbox = frappe.conf.get("hr_ai_hr_inbox") or "hr@rhflow.local"
    try:
        r = requests.get(f"{base}/api/v1/search", params={"query": f"to:{inbox}", "limit": limit}, timeout=30)
        r.raise_for_status()
    except requests.RequestException as exc:
        raise IngestError(f"mailbox unreachable ({type(exc).__name__})") from None
    messages = r.json().get("messages", [])
    ids = [m["ID"] for m in messages]
    done = set(frappe.get_all("Inbound Email", filters={"mailpit_id": ["in", ids]}, pluck="mailpit_id")) if ids else set()
    new = []
    for m in reversed(messages):  # oldest first
        if m["ID"] in done:
            continue
        raw = requests.get(f"{base}/api/v1/message/{m['ID']}/raw", timeout=30).content
        try:
            new.append(_ingest_mail(m["ID"], raw))
            frappe.db.commit()
        except frappe.DuplicateEntryError:
            frappe.db.rollback()  # another sync took it
    return {"new": len(new), "inbox": len(messages), "records": new}


def _ingest_mail(mailpit_id: str, raw: bytes) -> str:
    msg = email.message_from_bytes(raw, policy=default)
    sender = msg["From"].addresses[0].addr_spec if msg["From"] and msg["From"].addresses else ""
    message_id = (msg["Message-ID"] or "").strip()
    if not MESSAGE_ID.fullmatch(message_id):
        message_id = f"<{mailpit_id}@mailbox.rhflow.local>"
    in_reply_to = (msg["In-Reply-To"] or "").strip() or None
    if in_reply_to and not MESSAGE_ID.fullmatch(in_reply_to):
        in_reply_to = None
    subject = str(msg["Subject"] or "")
    part = msg.get_body(preferencelist=("html", "plain"))  # HTML first: the layer finds text hidden in it
    is_html = part is not None and part.get_content_type() == "text/html"
    body = part.get_content() if part is not None else ""
    # The subject is untrusted too: it goes through the same masking and scanning, as the first line.
    body = f"<p>Subject: {html.escape(subject)}</p>\n{body}" if is_html else f"Subject: {subject}\n\n{body}"

    record = {"doctype": "Inbound Email", "mailpit_id": mailpit_id, "received_on": now_datetime(),
              "sender_email": sender[:140], "subject": subject[:140], "message_id": message_id[:140]}
    if not SENDER.fullmatch(sender):
        record.update(verdict="error", released_to_ai=0)
    else:
        summary = _call("POST", "/v1/email", "hr_ai_ingest_upload_key", json={
            "body": body, "is_html": is_html, "sender_email": sender, "message_id": message_id,
            "in_reply_to": in_reply_to})
        attachments = []
        for att in msg.iter_attachments():
            ctype = att.get_content_type()
            if ctype not in CV_TYPES:
                attachments.append({"skipped": ctype})  # not a CV format: never forwarded to the AI
                continue
            a = _call("POST", "/v1/email/attachment", "hr_ai_ingest_upload_key",
                      files={"file": (att.get_filename() or f"attachment.{CV_TYPES[ctype]}", att.get_content())},
                      data={"sender_email": sender, "message_id": message_id})
            attachments.append({"report_id": a["report_id"], "doc_id": a["doc_id"], "verdict": verdict_of(a),
                                "released_to_ai": a["released_to_ai"]})
        record.update(report_id=summary["report_id"], doc_id=summary["doc_id"], verdict=verdict_of(summary),
                      released_to_ai=int(summary["released_to_ai"]),
                      attachments=json.dumps(attachments) if attachments else None,
                      job_applicant=frappe.db.get_value("Job Applicant", {"email_id": sender}, "name"))
    doc = frappe.get_doc(record)
    doc.flags.from_ingest = True
    doc.insert(ignore_permissions=True)
    if doc.released_to_ai:  # clean mail goes straight to the comms agent; held-back mail waits for HR
        from hr_ai.webhooks import notify_inbound_email
        notify_inbound_email(doc.name)
    return doc.name


# ---------------------------------------------------------------- Execution Gateway


def resolve_token(doc_id: str, token: str) -> str | None:
    """Real value behind <PERSON_1> in one released document (RESOLVER key: gateway only)."""
    try:
        return _call("POST", "/v1/resolve", "hr_ai_ingest_resolver_key", timeout=30,
                     json={"doc_id": doc_id, "token": token})["value"]
    except IngestError as exc:
        if exc.status == 404:
            return None
        raise


def masked_values(report_id: str) -> dict:
    """token -> original for one document, from its inspection report (REPORT key, HR display only)."""
    report = _call("GET", f"/v1/reports/{report_id}", "hr_ai_ingest_report_key", timeout=30)
    return {m["token"]: m["original"] for m in report.get("masked", [])}


# ---------------------------------------------------------------- HR "Security" tab (deciders only)


def _require_decider():
    if not DECIDERS & set(frappe.get_roles()):
        frappe.throw("Only HR Managers can open the security reports", frappe.PermissionError)


def _throw(exc: IngestError):
    frappe.throw(str(exc), title="Input security layer")


def _links() -> dict:
    """report_id -> the HR Flow record it belongs to."""
    links = {}
    for r in frappe.get_all("Inbound Email", fields=["name", "report_id", "attachments"]):
        if r.report_id:
            links[r.report_id] = ["Inbound Email", r.name]
        for a in json.loads(r.attachments or "[]"):
            if a.get("report_id"):
                links[a["report_id"]] = ["Inbound Email", r.name]
    for r in frappe.get_all("Job Applicant", filters={"rhflow_ingest_report_id": ["is", "set"]},
                            fields=["name", "rhflow_ingest_report_id"]):
        links[r.rhflow_ingest_report_id] = ["Job Applicant", r.name]
    for r in frappe.get_all("Chat Message", filters={"report_id": ["is", "set"]}, fields=["name", "report_id"]):
        links[r.report_id] = ["Chat Message", r.name]
    return links


@frappe.whitelist(methods=["GET"])
def security_reports():
    _require_decider()
    try:
        rows = _call("GET", "/v1/reports", "hr_ai_ingest_report_key")
    except IngestError as exc:
        _throw(exc)
    links = _links()
    for r in rows:
        r["linked"] = links.get(r["report_id"])
    return rows


@frappe.whitelist(methods=["GET"])
def security_report(report_id: str):
    _require_decider()
    if not REPORT_ID.fullmatch(report_id or ""):
        frappe.throw("Invalid report id")
    try:
        return _call("GET", f"/v1/reports/{report_id}", "hr_ai_ingest_report_key")
    except IngestError as exc:
        _throw(exc)


@frappe.whitelist(methods=["POST"])
def security_decide(report_id: str, action: str, reason: str):
    """HR releases a quarantined document to the AI (flagged as suspicious) or rejects a document."""
    _require_decider()
    if not REPORT_ID.fullmatch(report_id or "") or action not in ("release", "reject"):
        frappe.throw("Invalid request")
    if len((reason or "").strip()) < 3:
        frappe.throw("A reason is required")
    try:
        summary = _call("POST", f"/v1/reports/{report_id}/{action}", "hr_ai_ingest_report_key",
                        json={"reason": reason.strip()[:500]})
    except IngestError as exc:
        if exc.status == 409:
            frappe.throw("This document was already decided, or this decision does not apply to it.")
        _throw(exc)
    verdict = verdict_of(summary)
    link = _links().get(report_id)
    if link and link[0] == "Inbound Email":
        doc = frappe.get_doc("Inbound Email", link[1])
        if doc.report_id == report_id:
            frappe.db.set_value("Inbound Email", doc.name, {"verdict": verdict,
                                                          "released_to_ai": int(summary["released_to_ai"])})
        doc.add_comment("Info", f"Input security layer: {action}d by {frappe.session.user}. Reason: {reason.strip()[:500]}")
        if summary.get("released_to_ai") and doc.report_id == report_id:
            from hr_ai.webhooks import notify_inbound_email
            notify_inbound_email(doc.name)
    elif link and link[0] == "Chat Message":
        frappe.db.set_value("Chat Message", link[1], "verdict", verdict, update_modified=False)
    elif link:
        frappe.db.set_value("Job Applicant", link[1], "rhflow_ingest_verdict", verdict, update_modified=False)
        frappe.get_doc("Job Applicant", link[1]).add_comment(
            "Info", f"Input security layer (CV): {action}d by {frappe.session.user}. Reason: {reason.strip()[:500]}")
    return summary


@frappe.whitelist(methods=["POST"])
def security_sync():
    """HR button: pull new mail now and inspect any CV not yet inspected."""
    _require_decider()
    try:
        return {"inbox": sync_inbox(), "cvs": backfill_cvs()}
    except IngestError as exc:
        _throw(exc)
