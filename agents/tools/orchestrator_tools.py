"""Orchestrator tools: it can see what is pending and delegate; it has no domain tools of its own."""

from __future__ import annotations

from typing import Literal

from langchain_core.tools import tool

from agents.tools import outbox
from agents.tools.common import client, dataset, register_record, safe_tool

SpecialistName = Literal["recruitment", "comms", "assistant", "leave", "onboarding", "payroll", "performance",
                         "conflict"]


@tool
def delegate(agent: SpecialistName, task: str) -> str:
    """Hand a task to ONE specialist agent and get its result. Write the task as a clear instruction,
    including IDs (opening, applicant, application, employee) when known."""
    return "handled by the orchestrator graph"   # intercepted by the graph; never executed directly


@tool
@safe_tool
def list_pending_events() -> dict:
    """What is waiting for work: open leave applications, new job applicants, inbox size, proposals awaiting approval."""
    c = client()
    leave = c.get_list("Leave Application", filters=[["status", "=", "Open"], ["docstatus", "=", 0]],
                       fields=["name"], limit=200)
    applicants = c.get_list("Job Applicant", filters=[["status", "=", "Open"]], fields=["name", "job_title"], limit=500)
    by_opening: dict[str, int] = {}
    for a in applicants:
        by_opening[a["job_title"]] = by_opening.get(a["job_title"], 0) + 1
    result = {"open_leave_applications": len(leave), "open_applicants_by_opening": by_opening,
              "inbox_emails": len(list(dataset("emails").glob("*.eml"))),
              "proposals_awaiting_approval": len(outbox.list_actions("Proposed"))}
    result["source_hash"] = register_record("pending_events", result)
    return result


@tool
@safe_tool
def get_ai_action_status(action_id: str) -> dict:
    """Status of a proposal (AI Action) created earlier: Proposed, Approved, Rejected or Executed."""
    return outbox.get_action(action_id) or {"error": f"no AI Action {action_id}"}
