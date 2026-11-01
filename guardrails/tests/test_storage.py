import sqlite3
from pathlib import Path

import pytest

from guardrails.approval import ApprovalService, ApprovalSigner, Approver, Status
from guardrails.fairness import FairnessChecker
from guardrails.gateway import ExecutionGateway, GatewayError
from guardrails.models import ProposedAction, RecordRef
from guardrails.policy import PolicyEngine
from guardrails.storage import GENESIS, AuditLog, Database, SQLiteActionStore

from test_gateway import FakeFrappe

CFG = Path(__file__).resolve().parents[1] / "config"
KEY = b"test-key-0123456789-0123456789-0123456789"
HR = Approver("hr.manager@acme.test", frozenset({"HR Manager"}))
AGENT = Approver("svc.comms@acme.test", frozenset({"AI Agent"}))
APPLICANT = RecordRef("Job Applicant", "HR-APP-0001")


def build(db):
    return ApprovalService.from_file(
        CFG / "approval.json",
        policy=PolicyEngine.from_file(CFG / "policy.json"),
        fairness=FairnessChecker.from_file(CFG / "fairness.json"),
        signer=ApprovalSigner({"k1": KEY}, "k1"),
        store=SQLiteActionStore(db),
        audit=AuditLog(db),
    )


def email(action_id="a-1", recipients=None):
    recipients = recipients or [{"doctype": "Job Applicant", "name": "HR-APP-0001"}]
    return ProposedAction(action_id, "comms_agent", "send_email", APPLICANT,
                          {"recipients": recipients, "subject": "Your application",
                           "body": "Thank you for applying."},
                          ("sha256:src",), model="claude-sonnet-5-5", prompt_version="comms-v1")


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "g.db")
    yield d
    d.close()


def events(audit, action_id=None):
    return [e.event for e in audit.entries(action_id)]


# --- persistence --------------------------------------------------------------

def test_actions_survive_restart(tmp_path):
    path = tmp_path / "g.db"
    db1 = Database(path)
    svc = build(db1)
    rec = svc.submit(email())
    token = svc.approve("a-1", HR, rec.action_hash)
    db1.close()

    db2 = Database(path)  # "restart"
    svc2 = build(db2)
    assert svc2.store.get("a-1").status is Status.APPROVED
    assert svc2.verify("a-1", token).approver == HR.user
    assert svc2.audit.verify_chain().ok
    db2.close()


def test_record_roundtrip_keeps_everything(db):
    svc = build(db)
    svc.submit(email(), job_criteria=["python"])
    rec = svc.store.get("a-1")
    assert rec.action == email()
    assert rec.job_criteria == ("python",)


# --- audit trail content -----------------------------------------------------------

def test_full_lifecycle_is_audited(db):
    svc = build(db)
    gw = ExecutionGateway(svc, FakeFrappe())
    rec = svc.submit(email())
    token = svc.approve("a-1", HR, rec.action_hash)
    gw.execute("a-1", token, actor=HR.user)
    gw.execute("a-1", token, actor=HR.user)
    assert events(svc.audit, "a-1") == [
        "action.submitted", "action.approved", "execution.started",
        "execution.succeeded", "execution.duplicate",
    ]
    sub = svc.audit.entries("a-1")[0]
    assert sub.actor == "comms_agent"
    assert sub.details["model"] == "claude-sonnet-5-5"
    assert sub.details["prompt_version"] == "comms-v1"
    assert sub.details["source_hashes"] == ["sha256:src"]
    assert sub.details["policy_version"] == "2026-11-01.1"


def test_blocked_injection_is_audited_with_rule(db):
    svc = build(db)
    svc.submit(email(recipients=["attacker@evil.com"]))
    e = svc.audit.entries("a-1")[0]
    assert e.details["status"] == "Blocked"
    assert "recipient_not_a_record" in e.details["violations"]


def test_denied_approval_attempt_is_audited(db):
    svc = build(db)
    rec = svc.submit(email())
    with pytest.raises(Exception):
        svc.approve("a-1", AGENT, rec.action_hash)
    e = svc.audit.entries("a-1")[-1]
    assert e.event == "approval.denied" and e.details["code"] == "self_approval"
    assert e.actor == AGENT.user


def test_refused_execution_is_audited(db):
    svc = build(db)
    gw = ExecutionGateway(svc, FakeFrappe())
    svc.submit(email())
    with pytest.raises(GatewayError):
        gw.execute("a-1", "forged.token", actor="mallory")
    e = svc.audit.entries("a-1")[-1]
    assert e.event == "execution.refused" and e.actor == "mallory"


def test_edit_and_reject_are_audited(db):
    svc = build(db)
    rec = svc.submit(email())
    svc.approve("a-1", HR, rec.action_hash)
    svc.edit("a-1", {**rec.action.payload, "body": "Reworded by HR."}, HR)
    svc.reject("a-1", HR, "Not needed anymore")
    edited = svc.audit.entries("a-1")[2]
    assert edited.event == "action.edited" and edited.details["approval_voided"] is True
    assert events(svc.audit, "a-1")[-1] == "action.rejected"


def test_token_never_written_to_audit(db):
    svc = build(db)
    rec = svc.submit(email())
    token = svc.approve("a-1", HR, rec.action_hash)
    raw = db.conn.execute("SELECT group_concat(details) FROM audit_log").fetchone()[0]
    assert token not in raw and token.split(".")[1] not in raw


def test_email_body_not_written_to_audit(db):
    svc = build(db)
    svc.submit(email())
    raw = db.conn.execute("SELECT group_concat(details) FROM audit_log").fetchone()[0]
    assert "Thank you for applying" not in raw


# --- tamper evidence --------------------------------------------------------------

def _fill(db, n=5):
    audit = AuditLog(db)
    for i in range(n):
        audit.append("test.event", "tester", f"a-{i}", {"i": i})
    return audit


def test_chain_verifies(db):
    audit = _fill(db)
    rep = audit.verify_chain()
    assert rep.ok and rep.entries == 5 and rep.head == audit.head()


def test_empty_chain_head_is_genesis(db):
    assert AuditLog(db).head() == GENESIS and AuditLog(db).verify_chain().ok


def test_update_blocked_by_trigger(db):
    _fill(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("UPDATE audit_log SET actor = 'nobody' WHERE seq = 2")


def test_delete_blocked_by_trigger(db):
    _fill(db)
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        db.conn.execute("DELETE FROM audit_log WHERE seq = 2")


def _drop_triggers(db):
    """Simulate an attacker with full DB access who removes the triggers."""
    db.conn.execute("DROP TRIGGER audit_no_update")
    db.conn.execute("DROP TRIGGER audit_no_delete")


def test_modified_entry_detected_even_without_triggers(db):
    audit = _fill(db)
    _drop_triggers(db)
    db.conn.execute("""UPDATE audit_log SET details = '{"i":999}' WHERE seq = 3""")
    rep = audit.verify_chain()
    assert not rep.ok and rep.broken_at == 3 and "modified" in rep.reason


def test_deleted_middle_entry_detected(db):
    audit = _fill(db)
    _drop_triggers(db)
    db.conn.execute("DELETE FROM audit_log WHERE seq = 2")
    rep = audit.verify_chain()
    assert not rep.ok and rep.broken_at == 3


def test_rewritten_entry_with_recomputed_hash_breaks_next_link(db):
    """Attacker edits entry 2 AND recomputes its hash: entry 3 no longer links."""
    from guardrails.storage import _entry_hash
    audit = _fill(db)
    _drop_triggers(db)
    e = audit.entries()[1]
    new_hash = _entry_hash(e.seq, e.ts, e.event, "attacker", e.action_id, e.details, e.prev_hash)
    db.conn.execute("UPDATE audit_log SET actor = 'attacker', entry_hash = ? WHERE seq = 2", (new_hash,))
    rep = audit.verify_chain()
    assert not rep.ok and rep.broken_at == 3 and "link" in rep.reason


def test_truncated_tail_detected_with_published_head(db):
    audit = _fill(db)
    published = audit.head()
    _drop_triggers(db)
    db.conn.execute("DELETE FROM audit_log WHERE seq = 5")
    assert audit.verify_chain().ok                       # chain alone can't see it...
    assert not audit.verify_chain(expected_head=published).ok   # ...the published head can


# --- atomicity ---------------------------------------------------------------------

def test_failed_audit_rolls_back_state_change(db):
    svc = build(db)
    rec = svc.submit(email())

    def boom(*a, **k):
        raise RuntimeError("disk full")
    svc.audit.append = boom
    with pytest.raises(RuntimeError):
        svc.approve("a-1", HR, rec.action_hash)
    assert svc.store.get("a-1").status is Status.PROPOSED   # not approved without a log entry
