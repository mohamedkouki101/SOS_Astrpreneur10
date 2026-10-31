"""Read-only HR tools (Leave, HR Assistant, Onboarding, Payroll, Performance, Conflict) and their propose_* tools."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from langchain_core.tools import tool

from agents.context.run_context import current
from agents.tools import outbox
from agents.tools.common import (
    client,
    leave_balance,
    masked_text,
    register_record,
    safe_tool,
    search_policy_snippets,
    strip_personal,
    today,
)

LeaveType = Literal["Casual Leave", "Sick Leave", "Privilege Leave", "Compensatory Off", "Leave Without Pay"]


def _require_source(source_id: str) -> str | None:
    src = current().source_for(source_id)
    return src.sha256 if src else None


# ============================================================== shared
@tool
@safe_tool
def search_policies(query: str) -> list[dict]:
    """Search the company HR policies (leave, remote work, recruitment, conduct, expenses). Returns matching lines."""
    return search_policy_snippets(query)


@tool
@safe_tool
def get_holiday_list(year: int = 0) -> dict:
    """Public holidays for a year (default: this year), including the next upcoming holiday. Weekends excluded."""
    year = year or today().year
    lists = client().get_list("Holiday List", filters=[["from_date", "<=", f"{year}-12-31"],
                                                       ["to_date", ">=", f"{year}-01-01"]], fields=["name"])
    if not lists:
        return {"error": f"no holiday list for {year}"}
    doc = client().get_doc("Holiday List", lists[0]["name"])
    holidays = [{"date": h["holiday_date"], "name": h["description"]} for h in doc.get("holidays", [])
                if not h.get("weekly_off")]
    upcoming = [h for h in holidays if h["date"] >= str(today())]
    result = {"holiday_list": doc["name"], "year": year, "today": str(today()),
              "next_holiday": upcoming[0] if upcoming else None, "holidays": holidays}
    result["source_hash"] = register_record(f"Holiday List/{doc['name']}", result)
    return result


# ============================================================== HR Assistant (caller's own data only)
@tool
@safe_tool
def get_my_profile() -> dict:
    """The job profile of the employee who is asking (department, designation, branch, joining date)."""
    me = current().caller_employee
    if not me:
        return {"error": "no signed-in employee for this request"}
    doc = strip_personal(client().get_doc("Employee", me))
    keep = ["name", "department", "designation", "branch", "employment_type", "date_of_joining", "status",
            "reports_to", "holiday_list"]
    result = {k: doc.get(k) for k in keep}
    result["source_hash"] = register_record(f"Employee/{me}", result)
    return result


@tool
@safe_tool
def get_my_leave_balance(leave_type: LeaveType) -> dict:
    """Leave balance (allocated, taken, remaining this year) of the employee who is asking."""
    me = current().caller_employee
    if not me:
        return {"error": "no signed-in employee for this request"}
    return leave_balance(me, leave_type)


# ============================================================== Leave
@tool
@safe_tool
def list_leave_applications(status: Literal["Open", "Approved", "Rejected", "Cancelled"] = "Open",
                            limit: int = 20) -> list[dict]:
    """Leave applications with a given status (default: Open = waiting for a decision)."""
    rows = client().get_list("Leave Application", filters=[["status", "=", status], ["docstatus", "<", 2]],
                             fields=["name", "employee", "department", "leave_type", "from_date", "to_date",
                                     "total_leave_days", "posting_date", "status"],
                             order_by="from_date asc", limit=min(limit, 50))
    register_record(f"Leave Application list/{status}", rows)
    return rows


@tool
@safe_tool
def get_leave_application(application: str) -> dict:
    """One leave application with its (masked) reason and the employee's balance for that leave type."""
    doc = strip_personal(client().get_doc("Leave Application", application))
    keep = ["name", "employee", "department", "leave_type", "from_date", "to_date", "total_leave_days",
            "half_day", "posting_date", "status", "docstatus"]
    result = {k: doc.get(k) for k in keep}
    result["reason"] = masked_text(f"Leave Application/{application}/description", doc.get("description") or "")
    result["balance"] = leave_balance(doc["employee"], doc["leave_type"])
    result["source_hash"] = register_record(f"Leave Application/{application}", result)
    return result


@tool
@safe_tool
def get_leave_balance(employee: str, leave_type: LeaveType) -> dict:
    """Leave balance of one employee (by employee ID, e.g. HR-EMP-00007) for this year."""
    return leave_balance(employee, leave_type)


@tool
@safe_tool
def get_team_absences(department: str, date_from: dt.date, date_to: dt.date) -> dict:
    """Who in a department is on approved leave between two dates (employee IDs only)."""
    rows = client().get_list("Leave Application",
                             filters=[["department", "like", f"{department}%"], ["status", "=", "Approved"],
                                      ["docstatus", "=", 1], ["from_date", "<=", str(date_to)],
                                      ["to_date", ">=", str(date_from)]],
                             fields=["employee", "leave_type", "from_date", "to_date"], limit=100)
    result = {"department": department, "from": str(date_from), "to": str(date_to), "absences": rows}
    result["source_hash"] = register_record(f"Team absences/{department}/{date_from}/{date_to}", result)
    return result


@tool
@safe_tool
def propose_leave_decision(application: str, decision: Literal["Approved", "Rejected"], reason: str) -> dict:
    """PROPOSE approving or rejecting a leave application. A human must approve the proposal. Read the application first."""
    sha = _require_source(f"Leave Application/{application}")
    if not sha:
        return {"status": "REJECTED", "reason": "read the application with get_leave_application first"}
    return outbox.propose_action("leave_decision", f"Leave Application/{application}",
                                 {"application": application, "decision": decision, "reason": reason}, [sha])


# ============================================================== Onboarding
@tool
@safe_tool
def list_new_joiners(days: int = 60) -> list[dict]:
    """Employees who joined in the last N days (IDs and job info only)."""
    since = today() - dt.timedelta(days=min(days, 365))
    rows = client().get_list("Employee", filters=[["date_of_joining", ">=", str(since)], ["status", "=", "Active"]],
                             fields=["name", "department", "designation", "branch", "employment_type",
                                     "date_of_joining"], order_by="date_of_joining desc", limit=50)
    register_record(f"New joiners/{since}", rows)
    return rows


@tool
@safe_tool
def get_employee(employee: str) -> dict:
    """Job information of one employee by ID (no personal data)."""
    doc = strip_personal(client().get_doc("Employee", employee))
    keep = ["name", "department", "designation", "branch", "employment_type", "date_of_joining", "status",
            "reports_to"]
    result = {k: doc.get(k) for k in keep}
    result["source_hash"] = register_record(f"Employee/{employee}", result)
    return result


@tool
@safe_tool
def get_onboarding_template(designation: str) -> dict:
    """Onboarding activities defined for a designation (empty if no template exists)."""
    rows = client().get_list("Employee Onboarding Template", filters=[["designation", "=", designation]],
                             fields=["name"], limit=1)
    if not rows:
        return {"designation": designation, "activities": [], "note": "no template defined"}
    doc = client().get_doc("Employee Onboarding Template", rows[0]["name"])
    acts = [{"activity": a.get("activity_name"), "role": a.get("role"), "begin_on": a.get("begin_on"),
             "duration": a.get("duration")} for a in doc.get("activities", [])]
    result = {"designation": designation, "template": doc["name"], "activities": acts}
    result["source_hash"] = register_record(f"Onboarding Template/{doc['name']}", result)
    return result


@tool
@safe_tool
def propose_onboarding_tasks(employee: str, tasks: list[str]) -> dict:
    """PROPOSE an onboarding checklist for a new employee. Read the employee first with get_employee."""
    sha = _require_source(f"Employee/{employee}")
    if not sha:
        return {"status": "REJECTED", "reason": "read the employee with get_employee first"}
    return outbox.propose_action("onboarding_tasks", f"Employee/{employee}", {"employee": employee, "tasks": tasks},
                                 [sha])


@tool
@safe_tool
def propose_welcome_email(employee: str, subject: str, body: str) -> dict:
    """PROPOSE a welcome email for a new employee (refer to them as 'you', no names). Read the employee first."""
    sha = _require_source(f"Employee/{employee}")
    if not sha:
        return {"status": "REJECTED", "reason": "read the employee with get_employee first"}
    return outbox.propose_action("welcome_email", f"Employee/{employee}",
                                 {"employee": employee, "subject": subject, "body": body}, [sha])


# ============================================================== Payroll (read and explain only)
@tool
@safe_tool
def get_salary_slip(employee: str, month: str) -> dict:
    """Salary slip of an employee for a month 'YYYY-MM': gross, deductions, net and components."""
    start = f"{month}-01"
    rows = client().get_list("Salary Slip", filters=[["employee", "=", employee], ["start_date", ">=", start],
                                                     ["start_date", "<=", f"{month}-31"], ["docstatus", "<", 2]],
                             fields=["name"], limit=1)
    if not rows:
        return {"employee": employee, "month": month, "note": "no salary slip found for this month"}
    doc = client().get_doc("Salary Slip", rows[0]["name"])

    def comp(items):
        return [{"component": i.get("salary_component"), "amount": i.get("amount")} for i in items or []]

    result = {"employee": employee, "month": month, "slip": doc["name"], "currency": doc.get("currency"),
              "gross_pay": doc.get("gross_pay"), "total_deduction": doc.get("total_deduction"),
              "net_pay": doc.get("net_pay"), "payment_days": doc.get("payment_days"),
              "absent_days": doc.get("absent_days"), "leave_without_pay": doc.get("leave_without_pay"),
              "earnings": comp(doc.get("earnings")), "deductions": comp(doc.get("deductions"))}
    result["source_hash"] = register_record(f"Salary Slip/{doc['name']}", result)
    return result


@tool
@safe_tool
def get_attendance_summary(employee: str, month: str) -> dict:
    """Attendance of an employee for a month 'YYYY-MM': days per status."""
    rows = client().get_list("Attendance", filters=[["employee", "=", employee], ["docstatus", "=", 1],
                                                    ["attendance_date", ">=", f"{month}-01"],
                                                    ["attendance_date", "<=", f"{month}-31"]],
                             fields=["status"], limit=100)
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    result = {"employee": employee, "month": month, "days": counts}
    result["source_hash"] = register_record(f"Attendance/{employee}/{month}", result)
    return result


@tool
@safe_tool
def propose_payroll_reply(employee: str, body: str) -> dict:
    """PROPOSE a reply explaining a payslip or attendance question. Read the slip or attendance first. Never propose salary changes."""
    ctx = current()
    shas = [s.sha256 for s in ctx.sources.values()
            if s.source_id.startswith(("Salary Slip/", f"Attendance/{employee}/"))]
    if not shas:
        return {"status": "REJECTED", "reason": "read the salary slip or attendance first"}
    return outbox.propose_action("payroll_reply", f"Employee/{employee}", {"employee": employee, "body": body}, shas)


# ============================================================== Performance (summarise, never rate)
@tool
@safe_tool
def list_appraisals(cycle: str = "") -> list[dict]:
    """Appraisals, optionally for one appraisal cycle (IDs, status, scores)."""
    filters = [["docstatus", "<", 2]] + ([["appraisal_cycle", "=", cycle]] if cycle else [])
    rows = client().get_list("Appraisal", filters=filters,
                             fields=["name", "employee", "department", "designation", "appraisal_cycle",
                                     "final_score", "docstatus"], limit=50)
    register_record(f"Appraisal list/{cycle or 'all'}", rows)
    return rows


@tool
@safe_tool
def get_goals(employee: str) -> dict:
    """Goals of an employee with progress and status."""
    rows = client().get_list("Goal", filters=[["employee", "=", employee]],
                             fields=["name", "goal_name", "progress", "status", "kra", "appraisal_cycle",
                                     "start_date", "end_date"], limit=50)
    result = {"employee": employee, "goals": rows}
    result["source_hash"] = register_record(f"Goals/{employee}", result)
    return result


@tool
@safe_tool
def get_feedback(employee: str) -> dict:
    """Performance feedback received by an employee (feedback text is masked and scanned)."""
    rows = client().get_list("Employee Performance Feedback", filters=[["employee", "=", employee],
                                                                       ["docstatus", "=", 1]],
                             fields=["name", "appraisal_cycle", "total_score", "added_on"], limit=20)
    items = []
    for r in rows:
        doc = client().get_doc("Employee Performance Feedback", r["name"])
        items.append({**r, "feedback": masked_text(f"Feedback/{r['name']}", doc.get("feedback") or "")})
    result = {"employee": employee, "feedback": items}
    result["source_hash"] = register_record(f"Feedback/{employee}", result)
    return result


@tool
@safe_tool
def propose_review_summary(employee: str, summary: str) -> dict:
    """PROPOSE a neutral summary of an employee's goals and feedback for their manager. Never give a rating."""
    ctx = current()
    shas = [s.sha256 for s in ctx.sources.values() if s.source_id in (f"Goals/{employee}", f"Feedback/{employee}")]
    if not shas:
        return {"status": "REJECTED", "reason": "read goals or feedback first"}
    return outbox.propose_action("review_summary", f"Employee/{employee}",
                                 {"employee": employee, "summary": summary}, shas)


# ============================================================== Conflict (triage only)
@tool
@safe_tool
def list_grievances(status: Literal["Open", "Investigated", "Resolved", "Invalid"] = "Open") -> list[dict]:
    """Employee grievances with a status (IDs, type, date). Subjects are masked."""
    rows = client().get_list("Employee Grievance", filters=[["status", "=", status], ["docstatus", "<", 2]],
                             fields=["name", "grievance_type", "date", "status", "subject"], limit=50)
    for r in rows:
        r["subject"] = masked_text(f"Employee Grievance/{r['name']}/subject", r.get("subject") or "")
    register_record(f"Grievance list/{status}", rows)
    return rows


@tool
@safe_tool
def get_grievance(grievance: str) -> dict:
    """One grievance with its masked, scanned description."""
    doc = strip_personal(client().get_doc("Employee Grievance", grievance))
    result = {k: doc.get(k) for k in ("name", "grievance_type", "date", "status", "grievance_against_party")}
    result["description"] = masked_text(f"Employee Grievance/{grievance}/description", doc.get("description") or "")
    result["source_hash"] = register_record(f"Employee Grievance/{grievance}", result)
    return result


@tool
@safe_tool
def propose_grievance_triage(
    grievance: str,
    category: Literal["harassment", "discrimination", "workload", "pay", "management", "safety", "other"],
    priority: Literal["urgent", "high", "normal", "low"],
    next_step: str,
) -> dict:
    """PROPOSE a triage (category, priority, suggested next step) for HR. Never decide about people."""
    sha = _require_source(f"Employee Grievance/{grievance}")
    if not sha:
        return {"status": "REJECTED", "reason": "read the grievance with get_grievance first"}
    return outbox.propose_action("grievance_triage", f"Employee Grievance/{grievance}",
                                 {"grievance": grievance, "category": category, "priority": priority,
                                  "next_step": next_step}, [sha])
