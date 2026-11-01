"""HR-mode tools (HR Chat, the person chatting is an HR Manager): find records and PREPARE changes.

The operator agent reads with its own read-only API user and proposes changes; the HR user confirms
each change in the chat, and only then the Execution Gateway (hr_ai/executor.py) writes it.
Every propose_* tool requires the record to have been read in this run (its hash is cited).
Names stay masked: HR Core matches the names typed in the message to record ids before the agent
runs, so the model works with ids and [APPLICANT_n] references, never with real names.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Literal

from langchain_core.tools import tool

from agents.context.run_context import current
from agents.tools import outbox
from agents.tools.common import client, ref, register_record, safe_tool, strip_personal, unref

LeaveType = Literal["Casual Leave", "Sick Leave", "Privilege Leave", "Compensatory Off", "Leave Without Pay"]
EMPLOYEE_FIELDS = ["name", "department", "designation", "branch", "employment_type", "date_of_joining", "status",
                   "reports_to", "company"]


def _ensure_read(kind: str, record: str) -> None:
    """The propose tools read the record themselves if the model did not (still read-only, still cited)."""
    if current().source_for(f"{kind}/{record}") is None:
        {"Employee": get_employee, "Interview": get_interview}[kind].invoke({kind.lower(): record})


def _cite(*source_ids: str) -> list[str] | None:
    """Hashes of the records read in this run (+ the chat message itself). None if one is missing."""
    ctx, hashes = current(), []
    for sid in source_ids:
        src = ctx.source_for(sid)
        if src is None:
            return None
        hashes.append(src.sha256)
    if ctx.chat_message and (chat := ctx.source_for(f"chat/{ctx.chat_message}")):
        hashes.append(chat.sha256)
    return hashes


def _time(value) -> str | None:
    """'9:00', '09:00:00.000000' -> '09:00:00' (the model and the REST API both vary)."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", str(value or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}:{m.group(3) or '00'}" if m else None


def _interview_rows(applicant: str) -> list[dict]:
    rows = client().get_list("Interview", filters=[["job_applicant", "=", applicant]],
                             fields=["name", "interview_type", "scheduled_on", "from_time", "to_time", "status"],
                             order_by="scheduled_on asc", limit=20)
    for r in rows:
        r["from_time"], r["to_time"] = _time(r.get("from_time")), _time(r.get("to_time"))
        register_record(f"Interview/{r['name']}", r)
    return rows


@tool
@safe_tool
def get_employee(employee: str) -> dict:
    """Job information of one employee (ID like HR-EMP-00012): department, designation, branch, manager."""
    doc = strip_personal(client().get_doc("Employee", employee))
    result = {k: doc.get(k) for k in EMPLOYEE_FIELDS}
    result["source_hash"] = register_record(f"Employee/{employee}", result)
    return result


@tool
@safe_tool
def get_applicant(applicant: str) -> dict:
    """One job applicant (reference like [APPLICANT_3]): status, job opening and interviews."""
    real = unref(applicant)
    doc = client().get_doc("Job Applicant", real)
    token = ref("APPLICANT", real)
    result = {"applicant": token, "status": doc.get("status"), "job_opening": doc.get("job_title"),
              "interviews": _interview_rows(real)}
    result["source_hash"] = register_record(f"Job Applicant/{token}", result)
    return result


@tool
@safe_tool
def get_interview(interview: str) -> dict:
    """One interview by ID (e.g. HR-INT-2026-0003): type, date, times, status, applicant reference."""
    doc = client().get_doc("Interview", interview)
    result = {"name": doc["name"], "interview_type": doc.get("interview_type"), "scheduled_on": doc.get("scheduled_on"),
              "from_time": _time(doc.get("from_time")), "to_time": _time(doc.get("to_time")), "status": doc.get("status"),
              "applicant": ref("APPLICANT", doc["job_applicant"]) if doc.get("job_applicant") else None}
    result["source_hash"] = register_record(f"Interview/{interview}", result)
    return result


@tool
@safe_tool
def list_employee_leave(employee: str, status: Literal["Open", "Approved", "Rejected", "Cancelled"] = "Open") -> list[dict]:
    """Leave applications of one employee with a given status (default Open = waiting for a decision)."""
    rows = client().get_list("Leave Application", filters=[["employee", "=", employee], ["status", "=", status],
                                                           ["docstatus", "<", 2]],
                             fields=["name", "leave_type", "from_date", "to_date", "total_leave_days", "status"],
                             order_by="from_date asc", limit=20)
    register_record(f"Leave Application list/{employee}/{status}", rows)
    return rows


@tool
@safe_tool
def list_choices(kind: Literal["Department", "Designation", "Branch"]) -> list[str]:
    """Valid values for an employee's department, designation or branch."""
    rows = client().get_list(kind, fields=["name"], limit=200)
    names = [r["name"] for r in rows]
    register_record(f"{kind} list", names)
    return names


@tool
@safe_tool
def propose_interview_change(interview: str, action: Literal["reschedule", "cancel"], date: dt.date | None = None,
                             from_time: str | None = None, to_time: str | None = None, reason: str = "") -> dict:
    """PREPARE rescheduling (new date, optional new 'HH:MM' times) or cancelling an interview. Read it
    first (get_interview or get_applicant). Leave the times empty to keep the current times."""
    _ensure_read("Interview", interview)
    hashes = _cite(f"Interview/{interview}")
    if hashes is None:
        return {"status": "REJECTED", "reason": "read the interview first (get_interview or get_applicant)"}
    payload = {"interview": interview, "action": action, "reason": reason}
    if action == "reschedule":
        current_times = json.loads(current().source_for(f"Interview/{interview}").text)
        payload.update(date=str(date) if date else None,
                       from_time=_time(from_time) or current_times.get("from_time"),
                       to_time=_time(to_time) or current_times.get("to_time"))
    return outbox.propose_action("interview_update", f"Interview/{interview}", payload, hashes)


@tool
@safe_tool
def propose_applicant_status(applicant: str,
                             status: Literal["Open", "Replied", "Shortlisted", "Rejected", "Hold", "Accepted"],
                             reason: str = "") -> dict:
    """PREPARE changing a job applicant's status. Read the applicant first with get_applicant."""
    token = ref("APPLICANT", unref(applicant))
    if current().source_for(f"Job Applicant/{token}") is None:
        get_applicant.invoke({"applicant": token})
    hashes = _cite(f"Job Applicant/{token}")
    if hashes is None:
        return {"status": "REJECTED", "reason": "read the applicant first with get_applicant"}
    return outbox.propose_action("applicant_status", f"Job Applicant/{token}",
                                 {"applicant": token, "status": status, "reason": reason}, hashes)


@tool
@safe_tool
def propose_leave_application(employee: str, leave_type: LeaveType, from_date: dt.date, to_date: dt.date,
                              reason: str = "") -> dict:
    """PREPARE a new leave application (status Open) for an employee (ID like HR-EMP-00012)."""
    _ensure_read("Employee", employee)
    hashes = _cite(f"Employee/{employee}")
    if hashes is None:
        return {"status": "REJECTED", "reason": "read the employee first with get_employee"}
    return outbox.propose_action("leave_application", f"Employee/{employee}",
                                 {"employee": employee, "leave_type": leave_type, "from_date": str(from_date),
                                  "to_date": str(to_date), "reason": reason}, hashes)


@tool
@safe_tool
def propose_leave_decision(application: str, decision: Literal["Approved", "Rejected"], reason: str = "") -> dict:
    """PREPARE approving or rejecting an Open leave application (ID like HR-LAP-2026-00012)."""
    ctx = current()
    src = ctx.source_for(f"Leave Application/{application}")
    if src is None:   # read it here (read-only, cited) rather than asking the model to do it
        doc = client().get_doc("Leave Application", application)
        record = {k: doc.get(k) for k in ("name", "employee", "leave_type", "from_date", "to_date",
                                          "total_leave_days", "status", "docstatus")}
        register_record(f"Leave Application/{application}", record)
        src = ctx.source_for(f"Leave Application/{application}")
    record = json.loads(src.text)
    if record.get("status") != "Open" or record.get("docstatus", 0) != 0:
        return {"status": "REJECTED", "reason": f"this leave application is not open (status {record.get('status')})"}
    hashes = _cite(f"Leave Application/{application}")
    return outbox.propose_action("leave_decision", f"Leave Application/{application}",
                                 {"application": application, "decision": decision, "reason": reason}, hashes)


@tool
@safe_tool
def propose_employee_update(employee: str, department: str | None = None, designation: str | None = None,
                            branch: str | None = None, reports_to: str | None = None, reason: str = "") -> dict:
    """PREPARE changing an employee's job fields (department, designation, branch, reports_to = manager's
    employee ID). Use list_choices for valid values. Never personal data."""
    _ensure_read("Employee", employee)
    hashes = _cite(f"Employee/{employee}")
    if hashes is None:
        return {"status": "REJECTED", "reason": "read the employee first with get_employee"}
    payload = {"employee": employee, "reason": reason,
               **{k: v for k, v in {"department": department, "designation": designation, "branch": branch,
                                    "reports_to": reports_to}.items() if v}}
    return outbox.propose_action("employee_update", f"Employee/{employee}", payload, hashes)


TOOLS = [get_employee, get_applicant, get_interview, list_employee_leave, list_choices, propose_interview_change,
         propose_applicant_status, propose_leave_application, propose_leave_decision, propose_employee_update]
