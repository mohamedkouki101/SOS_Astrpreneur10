"""Allow rules for the HR Chat actions (agent `assistant` -> hr_request, agent `operator` -> HR-mode
changes). Each rule is explicit: the right agent, the right tool, ONE record that is both the AI
Action's reference and the record named in the payload. Everything else stays default-deny.
All data fictional."""
import json

import pytest

from guardrails.agent_layer import AgentLayerError
from guardrails.approval import Status

from test_agent_layer import H1, adapter, frappe, mutate, rules, submit, svc  # noqa: F401 (fixtures)

HR_REQUEST = {
    "kind": "hr_request", "agent": "assistant", "subject": "chat/CHAT-2026-00012",
    "reference_doctype": "Chat Message", "reference_name": "CHAT-2026-00012",
    "payload": {"message": "CHAT-2026-00012", "persona": "employee", "category": "document",
                "summary": "Asks for a work certificate for a bank appointment."},
    "source_hashes": [H1], "external_id": "AIA-chat000001",
}
INTERVIEW_UPDATE = {
    "kind": "interview_update", "agent": "operator", "subject": "Interview/HR-INT-2026-0009",
    "reference_doctype": "Interview", "reference_name": "HR-INT-2026-0009",
    "payload": {"interview": "HR-INT-2026-0009", "action": "reschedule", "date": "2026-12-12",
                "from_time": "09:00:00", "to_time": "09:45:00", "reason": "panel unavailable"},
    "source_hashes": [H1], "external_id": "AIA-chat000002",
}
LEAVE_APPLICATION = {
    "kind": "leave_application", "agent": "operator", "subject": "Employee/HR-EMP-00007",
    "reference_doctype": "Employee", "reference_name": "HR-EMP-00007",
    "payload": {"employee": "HR-EMP-00007", "leave_type": "Casual Leave", "from_date": "2026-12-21",
                "to_date": "2026-12-22", "reason": "family event"},
    "source_hashes": [H1], "external_id": "AIA-chat000003",
}
LEAVE_DECISION = {
    "kind": "leave_decision", "agent": "operator", "subject": "Leave Application/HR-LAP-2026-00027",
    "reference_doctype": "Leave Application", "reference_name": "HR-LAP-2026-00027",
    "payload": {"application": "HR-LAP-2026-00027", "decision": "Rejected", "reason": "peak period"},
    "source_hashes": [H1], "external_id": "AIA-chat000004",
}
EMPLOYEE_UPDATE = {
    "kind": "employee_update", "agent": "operator", "subject": "Employee/HR-EMP-00007",
    "reference_doctype": "Employee", "reference_name": "HR-EMP-00007",
    "payload": {"employee": "HR-EMP-00007", "branch": "Sfax", "reason": "transfer"},
    "source_hashes": [H1], "external_id": "AIA-chat000005",
}
OPERATOR_STATUS = {
    "kind": "applicant_status", "agent": "operator", "subject": "Job Applicant/[APPLICANT_1]",
    "reference_doctype": "Job Applicant", "reference_name": "cyrine.jlassi.sr2@example.com",
    "payload": {"applicant": "[APPLICANT_1]", "status": "Hold", "reason": "waiting for the second panel"},
    "token_map": {"[APPLICANT_1]": "cyrine.jlassi.sr2@example.com"},
    "source_hashes": [H1], "external_id": "AIA-chat000006",
}

ALLOWED = [
    (HR_REQUEST, "hr_assistant", "log_hr_request"),
    (INTERVIEW_UPDATE, "hr_operator", "update_interview"),
    (LEAVE_APPLICATION, "hr_operator", "create_leave_application"),
    (LEAVE_DECISION, "hr_operator", "decide_leave_application"),
    (EMPLOYEE_UPDATE, "hr_operator", "update_employee_job"),
    (OPERATOR_STATUS, "hr_operator", "update_applicant_status"),
]
BOUND = [(HR_REQUEST, "message"), (INTERVIEW_UPDATE, "interview"), (LEAVE_APPLICATION, "employee"),
         (LEAVE_DECISION, "application"), (EMPLOYEE_UPDATE, "employee")]


@pytest.mark.parametrize("record,agent,tool", ALLOWED, ids=lambda x: x if isinstance(x, str) else x["kind"])
def test_rh_chat_action_is_allowed(svc, adapter, record, agent, tool):
    rec, tr = submit(svc, adapter, record)
    assert rec.status is Status.PROPOSED, rec.violations
    assert (tr.action.agent, tr.action.tool) == (agent, tool)
    assert tr.action.subject.name == record["reference_name"]


@pytest.mark.parametrize("record,key", BOUND, ids=lambda x: x if isinstance(x, str) else x["kind"])
def test_payload_naming_another_record_is_blocked(svc, adapter, record, key):
    rec, _ = submit(svc, adapter, mutate(record, payload={key: "SOMEONE-ELSE-00001"}))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


@pytest.mark.parametrize("record,key", BOUND, ids=lambda x: x if isinstance(x, str) else x["kind"])
def test_missing_reference_is_blocked(svc, adapter, record, key):
    rec, _ = submit(svc, adapter, mutate(record, reference_doctype=None, reference_name=None))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


def test_reference_of_wrong_doctype_is_blocked(svc, adapter):
    # A leave decision pointed at an Employee record (the right name in the wrong table).
    rec, _ = submit(svc, adapter, mutate(LEAVE_DECISION, reference_doctype="Employee"))
    assert rec.status is Status.BLOCKED
    assert {"subject_mismatch", "subject_doctype"} <= rules(rec)


def test_employee_update_cannot_touch_personal_fields(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(EMPLOYEE_UPDATE, payload={"date_of_birth": "1990-01-01"}))
    assert rec.status is Status.BLOCKED
    assert {"agent_output_malformed", "protected_attribute"} <= rules(rec)


def test_employee_update_that_changes_nothing_is_blocked(svc, adapter):
    record = json.loads(json.dumps(EMPLOYEE_UPDATE))
    del record["payload"]["branch"]
    rec, _ = submit(svc, adapter, record)
    assert rec.status is Status.BLOCKED and "agent_output_malformed" in rules(rec)


def test_unknown_interview_action_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(INTERVIEW_UPDATE, payload={"action": "delete"}))
    assert rec.status is Status.BLOCKED and "agent_output_malformed" in rules(rec)


def test_record_bound_kinds_take_no_tokens(svc, adapter):
    rec, _ = submit(svc, adapter, {**EMPLOYEE_UPDATE, "token_map": {"[PERSON_1]": "Someone"}})
    assert rec.status is Status.BLOCKED and "token_value_not_from_record" in rules(rec)


def test_payload_missing_required_keys_is_rejected(adapter):
    record = json.loads(json.dumps(LEAVE_DECISION))
    del record["payload"]["decision"]
    from guardrails.agent_layer import AgentOutput
    with pytest.raises(AgentLayerError):
        adapter.translate(AgentOutput.parse(record))


@pytest.mark.parametrize("record", [INTERVIEW_UPDATE, LEAVE_APPLICATION, EMPLOYEE_UPDATE],
                         ids=lambda r: r["kind"])
def test_assistant_cannot_make_operator_changes(svc, adapter, record):
    rec, _ = submit(svc, adapter, mutate(record, agent="assistant"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


@pytest.mark.parametrize("agent", ["comms", "recruitment"])
def test_other_agents_cannot_change_employees(svc, adapter, agent):
    rec, _ = submit(svc, adapter, mutate(EMPLOYEE_UPDATE, agent=agent))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


def test_operator_cannot_send_email_or_rank(svc, adapter):
    from test_agent_layer import EMAIL_REPLY
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, agent="operator"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


def test_operator_status_for_another_applicant_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(OPERATOR_STATUS, token_map={"[APPLICANT_1]": "someone@example.com"}))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)
