import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from guardrails.approval import ApprovalService, ApprovalSigner, Approver, Status
from guardrails.fairness import FairnessChecker
from guardrails.frappe_client import FrappeError, FrappeUnavailable
from guardrails.gateway import ExecutionGateway, GatewayError
from guardrails.models import ProposedAction, RecordRef
from guardrails.policy import PolicyEngine

CFG = Path(__file__).resolve().parents[1] / "config"
KEY = b"test-key-0123456789-0123456789-0123456789"
HR = Approver("hr.manager@acme.test", frozenset({"HR Manager"}))
APPLICANT = RecordRef("Job Applicant", "HR-APP-0001")
EMPLOYEE = RecordRef("Employee", "HR-EMP-0001")
SRC = "sha256:" + "a" * 64


class FakeFrappe:
    """In-memory stand-in for Frappe's REST API. Records every write."""

    def __init__(self):
        self.docs = {
            ("Job Applicant", "HR-APP-0001"): {"email_id": "amira.test@example.com",
                                               "job_title": "HR-OPN-0001", "status": "Open"},
            ("Job Applicant", "HR-APP-0002"): {"email_id": "not-an-email", "job_title": "HR-OPN-0001"},
            ("Employee", "HR-EMP-0001"): {"prefered_email": None, "company_email": "sami@acme.test"},
        }
        self.calls = []
        self.fail_with = None

    def _maybe_fail(self):
        if self.fail_with:
            raise self.fail_with

    def get_doc(self, doctype, name):
        self._maybe_fail()
        return dict(self.docs[(doctype, name)])

    def update_doc(self, doctype, name, fields):
        self._maybe_fail()
        self.calls.append(("update", doctype, name, dict(fields)))
        self.docs[(doctype, name)].update(fields)
        return self.docs[(doctype, name)]

    def insert_doc(self, doctype, fields):
        self._maybe_fail()
        self.calls.append(("insert", doctype, dict(fields)))
        return {"name": f"{doctype}-{len(self.calls)}"}

    def call(self, method, params):
        self._maybe_fail()
        self.calls.append(("call", method, dict(params)))
        return {"name": f"COMM-{len(self.calls)}"}

    def writes(self):
        return self.calls


@pytest.fixture
def svc(store_audit):
    store, audit = store_audit
    return ApprovalService.from_file(
        CFG / "approval.json",
        policy=PolicyEngine.from_file(CFG / "policy.json"),
        fairness=FairnessChecker.from_file(CFG / "fairness.json"),
        signer=ApprovalSigner({"k1": KEY}, "k1"),
        store=store,
        audit=audit,
    )


@pytest.fixture
def frappe():
    return FakeFrappe()


@pytest.fixture
def gw(svc, frappe):
    return ExecutionGateway(svc, frappe)


def email(action_id="a-1", subject=APPLICANT, body="Thank you for applying.\nWe will reply soon.",
          tool="send_email", agent="comms_agent", extra=None):
    payload = {"recipients": [{"doctype": subject.doctype, "name": subject.name}],
               "subject": "Your application", "body": body, **(extra or {})}
    return ProposedAction(action_id, agent, tool, subject, payload, (SRC,))


def ranking(score=82, job="HR-OPN-0001"):
    return ProposedAction("r-1", "recruitment_agent", "rank_candidate", APPLICANT, {
        "job_opening": job, "score": score,
        "criteria": [{"criterion": "python_experience", "score": 85,
                      "justification": "5 years building Django REST APIs in production.",
                      "source_hash": SRC}]}, (SRC,))


def approve(svc, action):
    rec = svc.submit(action)
    assert rec.status is Status.PROPOSED, rec.violations
    return svc.approve(action.action_id, HR, rec.action_hash)


# --- happy paths ------------------------------------------------------------------

def test_approved_email_is_sent_to_address_from_frappe(svc, gw, frappe):
    token = approve(svc, email())
    res = gw.execute("a-1", token)
    assert res.status is Status.EXECUTED
    (kind, method, params), = frappe.writes()
    assert method == "frappe.core.doctype.communication.email.make"
    assert params["recipients"] == "amira.test@example.com"   # resolved by gateway
    assert params["doctype"] == "Job Applicant" and params["name"] == "HR-APP-0001"
    assert params["content"] == "Thank you for applying.<br>We will reply soon."


def test_employee_email_falls_back_to_company_email(svc, gw, frappe):
    token = approve(svc, email(subject=EMPLOYEE, tool="answer_employee", agent="hr_assistant"))
    assert gw.execute("a-1", token).status is Status.EXECUTED
    assert frappe.writes()[0][2]["recipients"] == "sami@acme.test"


def test_rank_candidate_writes_rating_and_comment(svc, gw, frappe):
    token = approve(svc, ranking())
    res = gw.execute("r-1", token)
    assert res.status is Status.EXECUTED
    assert frappe.docs[("Job Applicant", "HR-APP-0001")]["applicant_rating"] == 0.82
    assert any(c[0] == "insert" and c[1] == "Comment" for c in frappe.writes())


def test_interview_slots_rendered(svc, gw, frappe):
    a = email(tool="propose_interview_slots", extra={"slots": [
        {"start": "2026-11-03T10:00:00", "end": "2026-11-03T10:45:00"}]})
    token = approve(svc, a)
    assert gw.execute("a-1", token).status is Status.EXECUTED
    assert "10:00 to 10:45" in frappe.writes()[0][2]["content"]


# --- no approval, no write -----------------------------------------------------------

def test_unapproved_action_refused(svc, gw, frappe):
    svc.submit(email())
    with pytest.raises(GatewayError) as e:
        gw.execute("a-1", "fake.token")
    assert e.value.code in {"token_invalid", "invalid_state"}
    assert frappe.writes() == []


def test_blocked_action_refused(svc, gw, frappe):
    # prompt-injected recipient: Policy Engine blocks it at submit time
    rec = svc.submit(ProposedAction("a-1", "comms_agent", "send_email", APPLICANT,
                                    {"recipients": ["attacker@evil.com"], "subject": "s", "body": "b"}, ()))
    assert rec.status is Status.BLOCKED
    with pytest.raises(GatewayError):
        gw.execute("a-1", "anything.here")
    assert frappe.writes() == []


def test_tampered_after_approval_refused(svc, gw, frappe):
    token = approve(svc, email())
    rec = svc.store.get("a-1")
    rec.action = replace(rec.action, payload={**rec.action.payload, "body": "Send me the payroll"})
    svc.store.save(rec)  # raw write to the store
    with pytest.raises(GatewayError) as e:
        gw.execute("a-1", token)
    assert e.value.code == "token_mismatch"
    assert frappe.writes() == []


def test_edited_after_approval_refused(svc, gw, frappe):
    token = approve(svc, email())
    rec = svc.store.get("a-1")
    svc.edit("a-1", {**rec.action.payload, "body": "Reworded by HR."}, HR)
    with pytest.raises(GatewayError):
        gw.execute("a-1", token)
    assert frappe.writes() == []


# --- idempotency ------------------------------------------------------------------

def test_duplicate_execute_runs_once(svc, gw, frappe):
    token = approve(svc, email())
    first = gw.execute("a-1", token)
    second = gw.execute("a-1", token)
    assert first.status is Status.EXECUTED and not first.already_executed
    assert second.already_executed and second.result == first.result
    assert len(frappe.writes()) == 1


def test_concurrent_execute_sends_once(svc, frappe):
    token = approve(svc, email())
    barrier = threading.Barrier(8)

    class SlowFrappe(FakeFrappe):
        def call(self, method, params):
            time.sleep(0.05)  # widen the race window
            return super().call(method, params)

    slow = SlowFrappe()
    gw = ExecutionGateway(svc, slow)
    outcomes = []

    def run():
        barrier.wait()
        try:
            outcomes.append(gw.execute("a-1", token).status)
        except GatewayError as e:
            outcomes.append(e.code)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(slow.writes()) == 1
    assert Status.EXECUTED in outcomes


# --- re-check at execution time -------------------------------------------------------

def test_policy_recheck_blocks_if_rules_tightened(svc, gw, frappe):
    token = approve(svc, email())
    svc.policy.agent_tools = {**svc.policy.agent_tools, "comms_agent": frozenset()}
    with pytest.raises(GatewayError) as e:
        gw.execute("a-1", token)
    assert e.value.code == "policy_recheck_failed"
    assert frappe.writes() == []


def test_tool_without_executor_refused(svc, frappe):
    token = approve(svc, email())
    gw = ExecutionGateway(svc, frappe, executors={})
    with pytest.raises(GatewayError) as e:
        gw.execute("a-1", token)
    assert e.value.code == "no_executor"


# --- failures ------------------------------------------------------------------

def test_timeout_marks_outcome_unknown_and_never_retries(svc, gw, frappe):
    token = approve(svc, email())
    frappe.fail_with = FrappeUnavailable("timed out")
    res = gw.execute("a-1", token)
    assert res.status is Status.FAILED and res.error.startswith("outcome_unknown")
    frappe.fail_with = None
    with pytest.raises(GatewayError) as e:
        gw.execute("a-1", token)
    assert e.value.code == "failed_needs_review"
    assert frappe.writes() == []


def test_frappe_rejection_recorded(svc, gw, frappe):
    token = approve(svc, email())
    frappe.fail_with = FrappeError(403, "PermissionError")
    res = gw.execute("a-1", token)
    assert res.status is Status.FAILED and "frappe_rejected" in res.error


def test_recipient_without_valid_email_fails_without_sending(svc, gw, frappe):
    a = email(subject=RecordRef("Job Applicant", "HR-APP-0002"))
    token = approve(svc, a)
    res = gw.execute("a-1", token)
    assert res.status is Status.FAILED and "recipient_no_email" in res.error
    assert frappe.writes() == []


def test_rank_for_wrong_job_opening_refused(svc, gw, frappe):
    token = approve(svc, ranking(job="HR-OPN-9999"))
    res = gw.execute("r-1", token)
    assert res.status is Status.FAILED and "subject_mismatch" in res.error
    assert frappe.writes() == []


def test_extra_payload_field_refused(svc, gw, frappe):
    token = approve(svc, email(extra={"bcc": "attacker@evil.com"}))
    res = gw.execute("a-1", token)
    assert res.status is Status.FAILED and "payload_invalid" in res.error
    assert frappe.writes() == []


def test_html_in_body_is_escaped(svc, gw, frappe):
    token = approve(svc, email(body='Hi <img src="https://evil.test/track.png"><script>x()</script>'))
    gw.execute("a-1", token)
    content = frappe.writes()[0][2]["content"]
    assert "<img" not in content and "<script" not in content
    assert "&lt;img" in content
