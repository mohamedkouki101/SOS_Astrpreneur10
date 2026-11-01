"""Comms (inbox) tools.

INPUT_LAYER=service (the product path): the HR inbox is the list of Inbound Email records that RH Flow
created after sending each message through the input security layer. Content is the released, masked,
signed envelope; the sender is never visible to the model ("<SENDER>") and replies go back to the
original sender, resolved by the Execution Gateway.
INPUT_LAYER=local (offline fallback / tests): Mailpit or the dataset .eml folder, via the agents' own pipeline.

Every email goes through the 4-stage pipeline. The reply recipient is taken by code from the
email's masked From header (a token like [EMAIL_3]); the model cannot choose who receives a reply.
"""

from __future__ import annotations

import json
import re
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agents.context.run_context import Source, current
from agents.ingest import pipeline
from agents.ingest.pipeline import token_store
from agents.tools import input_layer, mailbox, outbox
from agents.tools.common import client, ref, register_record, safe_tool, search_policy_snippets, unref

SUBJECT_LINE = re.compile(r"^\s*Subject:[ \t]*(.*)$", re.MULTILINE)
INBOUND_FIELDS = ["name", "received_on", "verdict", "released_to_ai", "report_id", "job_applicant", "attachments"]


def _ingest_email(email_id: str) -> pipeline.IngestResult:
    email_id = mailbox.check_id(email_id)
    ctx = current()
    if (src := ctx.source_for(f"email/{email_id}")) is not None:
        return pipeline.IngestResult("ok", src.source_id, src.sha256, text=src.text, headers=src.meta["headers"])
    if input_layer.enabled():
        return _ingest_from_layer(client().get_doc("Inbound Email", email_id))
    res = pipeline.ingest_bytes(f"email/{email_id}", f"{email_id}.eml", mailbox.raw(email_id))
    if res.status == "ok":
        ctx.add_source(Source(source_id=res.source_id, sha256=res.sha256, text=res.text, kind="email",
                              meta={"headers": res.headers}))
    return res


def _ingest_from_layer(rec: dict) -> pipeline.IngestResult:
    """Inbound Email record -> verified envelope -> Source (what the model may read)."""
    source_id = f"email/{rec['name']}"
    if not rec.get("released_to_ai") or not rec.get("report_id"):
        return pipeline.IngestResult("quarantined", source_id, "", injection_hits=[
            f"held back by the input security layer (verdict: {rec.get('verdict') or 'unknown'})"])
    envelope = input_layer.fetch(rec["report_id"])
    if envelope is None:   # withdrawn by HR after release, or not released
        return pipeline.IngestResult("quarantined", source_id, "", injection_hits=["withdrawn by HR"])
    text = input_layer.text_of(envelope)
    subject = m.group(1).strip() if (m := SUBJECT_LINE.search(text)) else ""
    headers = {"From": "<SENDER>", "Subject": subject, "Date": str(rec.get("received_on") or "")}
    meta = {"headers": headers, "attachments": len(json.loads(rec.get("attachments") or "[]"))}
    known = {}
    if rec.get("job_applicant"):
        meta["applicant"] = ref("APPLICANT", rec["job_applicant"])
        app = client().get_doc("Job Applicant", rec["job_applicant"])
        known = {meta["applicant"]: app.get("applicant_name"), "[EMAIL]": app.get("email_id")}
    src = input_layer.to_source(source_id, envelope, "email", meta, known)
    current().add_source(src)
    return pipeline.IngestResult("ok", source_id, src.sha256, text=src.text, headers=headers)


def _inbox_ids(limit: int) -> list[str]:
    if input_layer.enabled():
        rows = client().get_list("Inbound Email", fields=["name"], order_by="received_on asc", limit=min(limit, 100))
        return [r["name"] for r in rows]
    return mailbox.list_ids(limit)


@tool
@safe_tool
def list_new_emails(limit: int = 30) -> list[dict]:
    """Emails in the HR inbox: id, masked sender, subject, date, and whether the email was quarantined."""
    out = []
    for email_id in _inbox_ids(limit):
        res = _ingest_email(email_id)
        if res.status == "ok":
            out.append({"email_id": email_id, "from": res.headers.get("From"), "subject": res.headers.get("Subject"),
                        "date": res.headers.get("Date"), "status": "ok"})
        else:
            out.append({"email_id": email_id, "status": "QUARANTINED", "reason": res.injection_hits or res.findings})
    return out


@tool
@safe_tool
def read_email(email_id: str) -> dict:
    """Read one email (masked, scanned). Quarantined emails return no content."""
    res = _ingest_email(email_id)
    if res.status != "ok":
        return {"email_id": email_id, "status": "QUARANTINED", "reason": res.injection_hits or res.findings}
    result = {"email_id": email_id, "status": "ok", "headers": res.headers, "body": res.text, "source_hash": res.sha256}
    src = current().source_for(f"email/{mailbox.check_id(email_id)}")
    if src and src.meta.get("layer") == "service":
        result["security_verdict"] = src.meta["verdict"]   # "suspicious": instructions were neutralised
        if src.meta.get("applicant"):
            result["applicant"] = src.meta["applicant"]     # linked application, if the sender applied
    return result


@tool
@safe_tool
def find_applicant_by_email(email_token: str) -> dict:
    """Find the job application linked to a masked sender token such as [EMAIL_3]."""
    if input_layer.enabled():
        return {"found": False, "note": "read_email already returns the linked 'applicant' when there is one"}
    original = token_store().original(email_token)
    if not original:
        return {"found": False}
    rows = client().get_list("Job Applicant", filters=[["email_id", "=", original]],
                             fields=["name", "status", "job_title"], limit=1)
    if not rows:
        return {"found": False}
    applicant = ref("APPLICANT", rows[0]["name"])
    result = {"found": True, "applicant": applicant, "status": rows[0]["status"], "opening": rows[0]["job_title"]}
    result["source_hash"] = register_record(f"Job Applicant/{applicant}", result)
    return result


@tool
@safe_tool
def get_applicant_status(applicant: str) -> dict:
    """Status of a job application and the title of the job opening."""
    doc = client().get_doc("Job Applicant", unref(applicant))
    applicant = ref("APPLICANT", doc["name"])
    op = client().get_doc("Job Opening", doc["job_title"])
    result = {"applicant": applicant, "status": doc.get("status"), "opening": op["name"], "job_title": op.get("job_title"),
              "opening_status": op.get("status"), "closes_on": op.get("closes_on")}
    result["source_hash"] = register_record(f"Job Applicant/{applicant}", result)
    return result


@tool
@safe_tool
def search_policies(query: str) -> list[dict]:
    """Search the company HR policies. Returns matching lines."""
    return search_policy_snippets(query)


@tool
@safe_tool
def propose_email_reply(email_id: str, subject: str, body: str) -> dict:
    """PROPOSE a reply to an email (read it first). Never write real names: keep placeholders exactly as they appear in the email (e.g. <PERSON_1> or [PERSON_1]); sign as "The RH Flow HR team". A human approves before sending."""
    ctx = current()
    src = ctx.source_for(f"email/{email_id}")
    if not src:
        return {"status": "REJECTED", "reason": "read the email with read_email first"}
    hashes = [src.sha256] + [s.sha256 for s in ctx.sources.values()
                             if s.source_id.startswith(("Job Applicant/", "policy/"))]
    if src.meta.get("layer") == "service":
        # Recipient = the original sender, resolved by the Execution Gateway; tokens are scoped to doc_id.
        subject, body, bad = _own_placeholders(src.text, subject, body)
        if bad:
            return {"status": "REJECTED",
                    "reason": f"{', '.join(bad)} do not appear in this email. Use only the placeholders "
                              "written in the email itself (like <PERSON_1>), or no name at all."}
        payload = {"email_id": email_id, "to": "<SENDER>", "subject": subject, "body": body,
                   "doc_id": src.meta["doc_id"]}
        return outbox.propose_action("email_reply", f"email/{email_id}", payload, hashes)
    to = re.search(r"\[EMAIL_\d+\]", src.meta.get("headers", {}).get("From", ""))
    if not to:
        return {"status": "REJECTED", "reason": "sender address could not be determined"}
    return outbox.propose_action("email_reply", f"email/{email_id}",
                                 {"email_id": email_id, "to": to.group(0), "subject": subject, "body": body}, hashes)


def _own_placeholders(email_text: str, *texts: str) -> tuple:
    """A reply may only use the placeholders of the email it answers. The model sometimes writes
    "[PERSON_1]" for the email's "<PERSON_1>": "[PERSON_1]" belongs to another token store and can
    stand for a different person, so it is switched back to "<PERSON_1>" when the email has that,
    and refused otherwise. Returns the texts, then the placeholders that are not in the email."""
    bad: set[str] = set()

    def fix(m: re.Match) -> str:
        token = m.group(0)
        if token in email_text:
            return token
        angle = f"<{token[1:-1]}>"
        if token.startswith("[") and angle in email_text:
            return angle
        bad.add(token)
        return token

    fixed = [re.sub(r"[\[<][A-Z]+_\d+[\]>]", fix, t or "") for t in texts]
    return (*fixed, sorted(bad))


def _email_exists(email_id: str) -> bool:
    if input_layer.enabled():
        try:
            return bool(client().get_doc("Inbound Email", mailbox.check_id(email_id)))
        except Exception:
            return False
    return mailbox.exists(email_id)


class DigestItemIn(BaseModel):
    email_id: str
    priority: Literal["high", "medium", "low"]
    category: Literal["candidate", "employee", "vendor", "internal", "security", "other"]
    summary: str = Field(max_length=300)


@tool
@safe_tool
def propose_digest(items: list[DigestItemIn]) -> dict:
    """PROPOSE an inbox digest: one item per email with priority, category and a one-line summary."""
    ctx = current()
    hashes, clean = [], []
    for it in items:
        it = it if isinstance(it, dict) else it.model_dump()
        src = ctx.source_for(f"email/{it['email_id']}")
        if src:
            hashes.append(src.sha256)
            clean.append(it)
        elif it.get("category") == "security" and _email_exists(it["email_id"]):
            clean.append(it)   # quarantined emails have no readable source; listed as security items
    return outbox.propose_action("digest", "inbox/digest", {"items": clean}, hashes)
