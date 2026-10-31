"""Comms (inbox) tools. The HR inbox is Mailpit (MAIL_SOURCE=mailpit) or the dataset .eml folder.

Every email goes through the 4-stage pipeline. The reply recipient is taken by code from the
email's masked From header (a token like [EMAIL_3]); the model cannot choose who receives a reply.
"""

from __future__ import annotations

import re
from typing import Literal

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from agents.context.run_context import Source, current
from agents.ingest import pipeline
from agents.ingest.pipeline import token_store
from agents.tools import mailbox, outbox
from agents.tools.common import client, ref, register_record, safe_tool, search_policy_snippets, unref


def _ingest_email(email_id: str) -> pipeline.IngestResult:
    email_id = mailbox.check_id(email_id)
    ctx = current()
    if (src := ctx.source_for(f"email/{email_id}")) is not None:
        return pipeline.IngestResult("ok", src.source_id, src.sha256, text=src.text, headers=src.meta["headers"])
    res = pipeline.ingest_bytes(f"email/{email_id}", f"{email_id}.eml", mailbox.raw(email_id))
    if res.status == "ok":
        ctx.add_source(Source(source_id=res.source_id, sha256=res.sha256, text=res.text, kind="email",
                              meta={"headers": res.headers}))
    return res


@tool
@safe_tool
def list_new_emails(limit: int = 30) -> list[dict]:
    """Emails in the HR inbox: id, masked sender, subject, date, and whether the email was quarantined."""
    out = []
    for email_id in mailbox.list_ids(limit):
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
    return {"email_id": email_id, "status": "ok", "headers": res.headers, "body": res.text, "source_hash": res.sha256}


@tool
@safe_tool
def find_applicant_by_email(email_token: str) -> dict:
    """Find the job application linked to a masked sender token such as [EMAIL_3]."""
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
    """PROPOSE a reply to an email (read it first). Use tokens like [PERSON_1] instead of names; sign as "The RH Flow HR team". A human approves before sending."""
    ctx = current()
    src = ctx.source_for(f"email/{email_id}")
    if not src:
        return {"status": "REJECTED", "reason": "read the email with read_email first"}
    to = re.search(r"\[EMAIL_\d+\]", src.meta.get("headers", {}).get("From", ""))
    if not to:
        return {"status": "REJECTED", "reason": "sender address could not be determined"}
    hashes = [src.sha256] + [s.sha256 for s in ctx.sources.values()
                             if s.source_id.startswith(("Job Applicant/", "policy/"))]
    return outbox.propose_action("email_reply", f"email/{email_id}",
                                 {"email_id": email_id, "to": to.group(0), "subject": subject, "body": body}, hashes)


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
        elif it.get("category") == "security" and mailbox.exists(it["email_id"]):
            clean.append(it)   # quarantined emails have no readable source; listed as security items
    return outbox.propose_action("digest", "inbox/digest", {"items": clean}, hashes)
