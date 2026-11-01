"""HR Chat tools for the HR Assistant: an employee's or a job applicant's OWN data, and one way to
hand a request to HR.

Who the person is comes from the run context (bound by code from the identities HR linked to the
chat account), never from tool arguments, so the model cannot look at anyone else's records.
"""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool

from agents.context.run_context import current
from agents.tools import outbox
from agents.tools.common import client, ref, register_record, safe_tool

Category = Literal["leave", "interview", "application", "document", "payroll", "complaint", "other"]


@tool
@safe_tool
def get_my_application() -> dict:
    """Status of the job application of the applicant who is asking: opening, status, interviews."""
    me = current().caller_applicant
    if not me:
        return {"error": "no job application is linked to this account"}
    app = client().get_doc("Job Applicant", me)
    result = {"applicant": ref("APPLICANT", me), "status": app.get("status"), "job_opening": app.get("job_title"),
              "applied_on": str(app.get("creation") or "")[:10]}
    if app.get("job_title"):
        job = client().get_doc("Job Opening", app["job_title"])
        result["position"] = job.get("job_title") or job.get("designation")
        result["opening_status"] = job.get("status")
    result["interviews"] = [
        {"type": i.get("interview_type"), "date": i.get("scheduled_on"), "from": i.get("from_time"),
         "to": i.get("to_time"), "status": i.get("status")}
        for i in client().get_list("Interview", filters=[["job_applicant", "=", me]],
                                   fields=["interview_type", "scheduled_on", "from_time", "to_time", "status"],
                                   order_by="scheduled_on asc", limit=10)
    ]
    result["source_hash"] = register_record(f"Job Applicant/{result['applicant']}", result)
    return result


@tool
@safe_tool
def list_open_jobs() -> list[dict]:
    """Job openings currently open (title, department, designation, closing date)."""
    rows = client().get_list("Job Opening", filters=[["status", "=", "Open"]],
                             fields=["name", "job_title", "designation", "department", "closes_on"], limit=30)
    register_record("Job Opening list/Open", rows)
    return rows


@tool
@safe_tool
def propose_hr_request(category: Category, summary: str) -> dict:
    """Pass a request the person made to HR (e.g. take leave, move an interview, get a document,
    report a problem). `summary`: 1-3 neutral sentences in English saying what the person asks.
    HR reviews it and follows up; nothing is done automatically."""
    ctx = current()
    if not ctx.chat_message or ctx.persona not in ("employee", "applicant"):
        return {"error": "requests can only be passed to HR for a signed-in employee or applicant"}
    src = ctx.source_for(f"chat/{ctx.chat_message}")
    if src is None:
        return {"error": "the chat message was not read in this run"}
    payload = {"message": ctx.chat_message, "persona": ctx.persona, "category": category, "summary": summary}
    return outbox.propose_action("hr_request", f"chat/{ctx.chat_message}", payload, [src.sha256])
