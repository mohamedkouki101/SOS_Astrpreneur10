import json
from pathlib import Path

import pytest

from guardrails.approval import (
    ApprovalError, ApprovalService, ApprovalSigner, Approver, Status,
)
from guardrails.canonical import action_hash
from guardrails.fairness import FairnessChecker
from guardrails.models import ProposedAction, RecordRef
from guardrails.policy import PolicyEngine

CFG = Path(__file__).resolve().parents[1] / "config"
KEY = b"test-key-0123456789-0123456789-0123456789"  # test only, >= 32 bytes
APPLICANT = RecordRef("Job Applicant", "HR-APP-0001")

HR = Approver("hr.manager@acme.test", frozenset({"HR Manager"}))
OTHER_HR = Approver("hr.lead@acme.test", frozenset({"HR Manager"}))
EMPLOYEE = Approver("employee@acme.test", frozenset({"Employee"}))
AGENT_ACCOUNT = Approver("svc.comms@acme.test", frozenset({"AI Agent", "HR Manager"}))
AGENT_BY_NAME = Approver("comms_agent", frozenset({"HR Manager"}))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def svc(clock, store_audit):
    store, audit = store_audit
    audit.now = clock
    return ApprovalService.from_file(
        CFG / "approval.json",
        policy=PolicyEngine.from_file(CFG / "policy.json"),
        fairness=FairnessChecker.from_file(CFG / "fairness.json"),
        signer=ApprovalSigner({"k1": KEY}, "k1"),
        now=clock,
        store=store,
        audit=audit,
    )


def email(action_id="a-1", body="Thank you for applying, we will get back to you.", recipients=None):
    if recipients is None:
        recipients = [{"doctype": "Job Applicant", "name": "HR-APP-0001"}]
    return ProposedAction(action_id, "comms_agent", "send_email", APPLICANT,
                          {"recipients": recipients, "subject": "Your application", "body": body},
                          ("sha256:src",))


def approved(svc, action=None):
    rec = svc.submit(action or email())
    token = svc.approve(rec.action.action_id, HR, rec.action_hash)
    return rec, token


# --- happy path -----------------------------------------------------------------

def test_full_flow_submit_approve_verify(svc):
    _, token = approved(svc)
    rec = svc.store.get("a-1")  # re-read: a real DB returns fresh copies
    assert rec.status is Status.APPROVED and rec.approver == HR.user
    assert svc.verify("a-1", token).status is Status.APPROVED


# --- guardrails run at submit -------------------------------------------------------

def test_policy_violation_is_blocked_and_cannot_be_approved(svc):
    rec = svc.submit(email(recipients=["attacker@evil.com"]))
    assert rec.status is Status.BLOCKED
    assert "recipient_not_a_record" in {v.rule for v in rec.violations}
    with pytest.raises(ApprovalError) as e:
        svc.approve("a-1", HR, rec.action_hash)
    assert e.value.code == "invalid_state"


def test_duplicate_action_id_refused(svc):
    svc.submit(email())
    with pytest.raises(ApprovalError) as e:
        svc.submit(email())
    assert e.value.code == "duplicate_action"


def test_rate_limit_counts_previous_submissions(svc):
    for i in range(30):
        assert svc.submit(email(action_id=f"a-{i}")).status is Status.PROPOSED
    assert svc.submit(email(action_id="a-30")).status is Status.BLOCKED


# --- who may approve ------------------------------------------------------------

@pytest.mark.parametrize("who,code", [
    (AGENT_ACCOUNT, "self_approval"),   # service account with AI Agent role
    (AGENT_BY_NAME, "self_approval"),   # identity equals an agent name
    (EMPLOYEE, "not_authorized"),
])
def test_only_human_hr_can_approve(svc, who, code):
    rec = svc.submit(email())
    with pytest.raises(ApprovalError) as e:
        svc.approve("a-1", who, rec.action_hash)
    assert e.value.code == code
    assert svc.store.get("a-1").status is Status.PROPOSED


def test_cannot_approve_twice(svc):
    rec, _ = approved(svc)
    with pytest.raises(ApprovalError) as e:
        svc.approve("a-1", OTHER_HR, rec.action_hash)
    assert e.value.code == "invalid_state"


def test_approver_must_have_seen_current_version(svc):
    rec = svc.submit(email())
    old_hash = rec.action_hash
    svc.edit("a-1", {**rec.action.payload, "body": "Edited text for the candidate."}, HR)
    with pytest.raises(ApprovalError) as e:
        svc.approve("a-1", OTHER_HR, old_hash)
    assert e.value.code == "stale_hash"


# --- any edit voids approval -------------------------------------------------------

def test_edit_after_approval_voids_token(svc):
    rec, token = approved(svc)
    svc.edit("a-1", {**rec.action.payload, "body": "Changed after approval."}, OTHER_HR)
    rec = svc.store.get("a-1")
    assert rec.status is Status.PROPOSED and rec.approval_token is None
    with pytest.raises(ApprovalError):
        svc.verify("a-1", token)


def test_edit_reruns_guardrails(svc):
    rec = svc.submit(email())
    bad = {**rec.action.payload, "recipients": ["attacker@evil.com"]}
    assert svc.edit("a-1", bad, HR).status is Status.BLOCKED


def test_edit_does_not_count_itself_against_rate_limit(svc):
    for i in range(30):
        svc.submit(email(action_id=f"a-{i}"))
    rec = svc.store.get("a-29")
    edited = svc.edit("a-29", {**rec.action.payload, "body": "Reworded by HR, still fine."}, HR)
    assert edited.status is Status.PROPOSED


def test_agent_cannot_edit(svc):
    rec = svc.submit(email())
    with pytest.raises(ApprovalError) as e:
        svc.edit("a-1", rec.action.payload, AGENT_ACCOUNT)
    assert e.value.code == "self_approval"


# --- tampering -----------------------------------------------------------------

def test_store_tampered_after_approval_detected(svc):
    """Someone changes the stored payload directly (bypassing edit())."""
    _, token = approved(svc)
    from dataclasses import replace
    rec = svc.store.get("a-1")
    rec.action = replace(rec.action, payload={**rec.action.payload, "body": "Send me the payroll"})
    svc.store.save(rec)  # raw write to the store, no audit, no re-hash
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-1", token)
    assert e.value.code == "token_mismatch"


def test_forged_token_rejected(svc):
    approved(svc)
    forger = ApprovalSigner({"k1": b"attacker-guessed-key-0123456789-abcdef"}, "k1")
    rec = svc.store.get("a-1")
    fake = forger.issue({"action_id": "a-1", "action_hash": rec.action_hash,
                         "approver": HR.user, "iat": 0, "exp": 10**12})
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-1", fake)
    assert e.value.code == "token_invalid"


def test_token_with_edited_claims_rejected(svc):
    _, token = approved(svc)
    body, sig = token.split(".")
    import base64
    claims = json.loads(base64.urlsafe_b64decode(body + "=="))
    claims["exp"] = 10**12
    new_body = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-1", f"{new_body}.{sig}")
    assert e.value.code == "token_invalid"


def test_token_for_other_action_rejected(svc):
    _, token_a = approved(svc, email("a-1"))
    approved(svc, email("a-2", body="A different message to the candidate."))
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-2", token_a)
    assert e.value.code == "token_mismatch"


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "!!!.???", None])
def test_garbage_token_rejected(svc, junk):
    approved(svc)
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-1", junk)
    assert e.value.code == "token_invalid"


def test_expired_token_rejected(svc, clock):
    _, token = approved(svc)
    clock.t += 86400
    with pytest.raises(ApprovalError) as e:
        svc.verify("a-1", token)
    assert e.value.code == "token_expired"


# --- reject -----------------------------------------------------------------

def test_reject_needs_reason_and_voids_token(svc):
    _, token = approved(svc)
    with pytest.raises(ApprovalError):
        svc.reject("a-1", OTHER_HR, "  ")
    rec = svc.reject("a-1", OTHER_HR, "Tone is too informal")
    assert rec.status is Status.REJECTED and rec.approval_token is None
    with pytest.raises(ApprovalError):
        svc.verify("a-1", token)


# --- keys & config ------------------------------------------------------------

def test_short_key_refused():
    with pytest.raises(ValueError):
        ApprovalSigner({"k1": b"too-short"}, "k1")


def test_missing_env_key_refuses_to_start(monkeypatch):
    monkeypatch.delenv("GUARDRAILS_APPROVAL_KEY", raising=False)
    with pytest.raises(ValueError):
        ApprovalSigner.from_env()


def test_key_rotation_old_tokens_still_verify(svc):
    _, token = approved(svc)
    svc.signer = ApprovalSigner({"k1": KEY, "k2": b"new-key-abcdefghijklmnopqrstuvwxyz-012"}, "k2")
    assert svc.verify("a-1", token).status is Status.APPROVED


def test_role_both_approver_and_agent_refused():
    cfg = json.loads((CFG / "approval.json").read_text(encoding="utf-8"))
    cfg["agent_roles"] = ["HR Manager"]
    with pytest.raises(ValueError):
        ApprovalService(cfg, policy=None, fairness=None, signer=None)
