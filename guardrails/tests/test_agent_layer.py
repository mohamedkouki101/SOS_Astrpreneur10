"""The guardrails fed with the AI agent layer's own output format (hr_ai AI Action).

Records below have the exact shape of the proposals made by the team's agents
(all data fictional)."""
import copy
import json
from pathlib import Path

import pytest

from guardrails.agent_layer import (
    AgentLayerAdapter, AgentLayerError, AgentOutput, FrappeDirectory,
)
from guardrails.approval import ApprovalService, ApprovalSigner, Approver, Status
from guardrails.fairness import FairnessChecker
from guardrails.frappe_client import FrappeUnavailable
from guardrails.gateway import ExecutionGateway
from guardrails.policy import PolicyEngine

from test_gateway import FakeFrappe

CFG = Path(__file__).resolve().parents[1] / "config"
KEY = b"test-key-0123456789-0123456789-0123456789"
HR = Approver("hr.manager@acme.test", frozenset({"HR Manager"}))
H1 = "bcee57a58f42ad9bbd3c92520295b83c546b241fb23109042b3f5957225869b5"
H2 = "c7e5f3c72c4d6fb125ea06c49c56f2f7cf283ebe8e1dae114834716f59dc863c"

EMAIL_REPLY = {   # shape of AIA-2026-00005
    "kind": "email_reply", "agent": "comms", "subject": "email/010_candidate",
    "reference_doctype": None, "reference_name": None,
    "summary": "To: [EMAIL_1] Subject: Question about the Sales Representative position",
    "payload": json.dumps({
        "email_id": "010_candidate", "to": "[EMAIL_1]",
        "subject": "Question about the Sales Representative position",
        "body": "Dear [PERSON_1],\n\nThank you for your interest in the Sales Representative position."}),
    "source_hashes": json.dumps([H1]),
    "token_map": json.dumps({"[PERSON_1]": "Cyrine Jlassi", "[EMAIL_1]": "cyrine.jlassi.sr2@example.com"}),
    "action_hash": "84c22d32235fc7dc4ffe82e2b6bc11a3e9610ab9f59e3ae386fd508c4d48cacf",
    "external_id": "AIA-6ec528d223", "run_id": "c4ba4f109cb8",
}

INTERVIEW = {     # shape of AIA-2026-00004
    "kind": "interview", "agent": "recruitment", "subject": "Job Applicant/[APPLICANT_1]",
    "reference_doctype": "Job Applicant", "reference_name": "imen.baccouche.dm7@example.com",
    "summary": "HR Screening with [APPLICANT_1] 2026-11-02  09:00 - 09:45",
    "payload": {"applicant": "[APPLICANT_1]", "interview_type": "HR Screening",
                "date": "2026-11-02", "from_time": "09:00", "to_time": "09:45"},
    "source_hashes": [H2, H1],
    "token_map": {"[APPLICANT_1]": "imen.baccouche.dm7@example.com"},
    "external_id": "AIA-9540026bd2", "run_id": "240a699513ff",
}


def mutate(record, **changes):
    r = copy.deepcopy(record)
    for k, v in changes.items():
        if k in ("payload", "token_map") and isinstance(r[k], str):
            base = json.loads(r[k])
            base.update(v)
            r[k] = json.dumps(base)
        elif k in ("payload", "token_map"):
            r[k] = {**r[k], **v}
        else:
            r[k] = v
    return r


@pytest.fixture
def frappe():
    return FakeFrappe()


@pytest.fixture
def adapter(frappe):
    cfg = json.loads((CFG / "agent_layer.json").read_text(encoding="utf-8"))
    return AgentLayerAdapter(cfg, FrappeDirectory(frappe))


@pytest.fixture
def svc():
    return ApprovalService.from_file(
        CFG / "approval.json", policy=PolicyEngine.from_file(CFG / "policy.json"),
        fairness=FairnessChecker.from_file(CFG / "fairness.json"),
        signer=ApprovalSigner({"k1": KEY}, "k1"))


def submit(svc, adapter, record):
    tr = adapter.translate(AgentOutput.parse(record))
    return svc.submit(tr.action, extra_violations=tr.violations), tr


def rules(rec):
    return {v.rule for v in rec.violations}


# --- legitimate proposals now pass ---------------------------------------------------

def test_real_email_reply_is_translated_and_allowed(svc, adapter):
    rec, tr = submit(svc, adapter, EMAIL_REPLY)
    assert rec.status is Status.PROPOSED, rec.violations
    a = tr.action
    assert (a.agent, a.tool) == ("comms_agent", "send_email")
    assert a.action_id == "AIA-6ec528d223"
    assert a.subject.doctype == "Job Applicant" and a.subject.name == "cyrine.jlassi.sr2@example.com"
    assert a.payload["recipients"] == [{"doctype": "Job Applicant", "name": "cyrine.jlassi.sr2@example.com"}]
    assert a.source_hashes == ("sha256:" + H1,)


def test_email_reply_executes_with_names_from_frappe(svc, adapter, frappe):
    rec, _ = submit(svc, adapter, EMAIL_REPLY)
    token = svc.approve(rec.action.action_id, HR, rec.action_hash)
    res = ExecutionGateway(svc, frappe).execute(rec.action.action_id, token)
    assert res.status is Status.EXECUTED, res.error
    (_, _, params), = frappe.writes()
    assert params["recipients"] == "cyrine.jlassi.sr2@example.com"
    assert params["content"].startswith("Dear Cyrine Jlassi,")
    assert "[PERSON_1]" not in params["content"]


def test_real_interview_is_allowed_and_creates_interview(svc, adapter, frappe):
    rec, tr = submit(svc, adapter, INTERVIEW)
    assert rec.status is Status.PROPOSED, rec.violations
    assert (tr.action.agent, tr.action.tool) == ("recruitment_agent", "schedule_interview")
    token = svc.approve(rec.action.action_id, HR, rec.action_hash)
    res = ExecutionGateway(svc, frappe).execute(rec.action.action_id, token)
    assert res.status is Status.EXECUTED, res.error
    kind, doctype, fields = frappe.writes()[0]
    assert (kind, doctype) == ("insert", "Interview")
    assert fields == {"interview_type": "HR Screening", "job_applicant": "imen.baccouche.dm7@example.com",
                      "scheduled_on": "2026-11-02", "from_time": "09:00", "to_time": "09:45",
                      "status": "Pending"}


# --- attacks in the agent layer's format ------------------------------------------------

def test_token_map_swapped_to_attacker_is_blocked(svc, adapter):
    """HR would still read 'To: [EMAIL_1]' in Desk: the swap is invisible to a human."""
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, token_map={"[EMAIL_1]": "attacker@evil-mail.test"}))
    assert rec.status is Status.BLOCKED
    assert {"recipient_unknown", "recipient_not_a_record"} <= rules(rec)


def test_raw_attacker_address_in_to_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, payload={"to": "attacker@evil-mail.test"}))
    assert rec.status is Status.BLOCKED and "recipient_unknown" in rules(rec)


def test_phishing_hidden_behind_person_token_is_blocked(svc, adapter):
    """'Dear [PERSON_1]' looks harmless, but [PERSON_1] would become a phishing line."""
    evil = {"[PERSON_1]": "Cyrine, urgent: confirm your bank details at http://evil.test/pay"}
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, token_map=evil))
    assert rec.status is Status.BLOCKED and "token_value_not_from_record" in rules(rec)


def test_email_redirected_to_another_known_applicant_is_blocked(svc, adapter):
    """[EMAIL_1] points to a real applicant, but [PERSON_1] is someone else's name:
    a reply about one candidate sent to another."""
    swap = {"[EMAIL_1]": "imen.baccouche.dm7@example.com"}
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, token_map=swap))
    assert rec.status is Status.BLOCKED and "token_value_not_from_record" in rules(rec)


def test_token_missing_from_map_is_blocked(svc, adapter):
    r = mutate(EMAIL_REPLY)
    r["token_map"] = json.dumps({"[PERSON_1]": "Cyrine Jlassi"})
    rec, _ = submit(svc, adapter, r)
    assert rec.status is Status.BLOCKED and "unresolved_token" in rules(rec)


def test_interview_for_a_different_applicant_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(INTERVIEW, token_map={"[APPLICANT_1]": "cyrine.jlassi.sr2@example.com"}))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


def test_interview_by_comms_agent_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(INTERVIEW, agent="comms"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


def test_unknown_kind_is_denied_by_default(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, kind="leave_decision", external_id="AIA-x1"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


def test_unmapped_agent_is_denied(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, agent="orchestrator"))
    assert rec.status is Status.BLOCKED and "unknown_agent" in rules(rec)


def test_bad_source_hash_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(EMAIL_REPLY, source_hashes=["not-a-hash"]))
    assert rec.status is Status.BLOCKED and "bad_source_hash" in rules(rec)


def test_ambiguous_email_is_refused(svc, adapter, frappe):
    frappe.docs[("Job Applicant", "dup")] = {"email_id": "cyrine.jlassi.sr2@example.com",
                                             "applicant_name": "Cyrine Jlassi"}
    rec, _ = submit(svc, adapter, EMAIL_REPLY)
    assert rec.status is Status.BLOCKED and "recipient_unknown" in rules(rec)


def test_frappe_down_fails_closed(svc, adapter, frappe):
    frappe.fail_with = FrappeUnavailable("down")
    rec, _ = submit(svc, adapter, EMAIL_REPLY)
    assert rec.status is Status.BLOCKED and "directory_unavailable" in rules(rec)


def test_record_changed_after_approval_refused_at_send(svc, adapter, frappe):
    """Defence in depth: the gateway re-checks token values against Frappe at send time."""
    rec, _ = submit(svc, adapter, EMAIL_REPLY)
    token = svc.approve(rec.action.action_id, HR, rec.action_hash)
    frappe.docs[("Job Applicant", "cyrine.jlassi.sr2@example.com")]["applicant_name"] = "Someone Else"
    res = ExecutionGateway(svc, frappe).execute(rec.action.action_id, token)
    assert res.status is Status.FAILED and "token_value_not_from_record" in res.error
    assert frappe.writes() == []


# --- parsing ---------------------------------------------------------------------

def test_json_string_and_object_fields_both_accepted():
    a = AgentOutput.parse(EMAIL_REPLY)           # payload/token_map/source_hashes as JSON strings
    b = AgentOutput.parse(INTERVIEW)             # ... as objects
    assert a.payload["to"] == "[EMAIL_1]" and b.payload["applicant"] == "[APPLICANT_1]"


@pytest.mark.parametrize("bad", [
    {k: v for k, v in EMAIL_REPLY.items() if k != "kind"},
    {**EMAIL_REPLY, "approver": "Administrator"},          # agent must not set decision fields
    {**EMAIL_REPLY, "payload": "{not json"},
    {**EMAIL_REPLY, "token_map": {"PERSON_1": "x"}},        # not a [TOKEN_n]
    {**EMAIL_REPLY, "external_id": "../../etc"},
])
def test_malformed_agent_output_refused(bad):
    with pytest.raises(AgentLayerError):
        AgentOutput.parse(bad)


# --- hr_ai formats: <SENDER> replies, applicant status, ranking list, digest ------------

DOC = "sha256:" + H1
SENDER_REPLY = {   # reply to an inbox email processed by the input security layer
    "kind": "email_reply", "agent": "comms", "subject": "email/IN-2026-00004",
    "reference_doctype": "Inbound Email", "reference_name": "IN-2026-00004",
    "payload": {"email_id": "IN-2026-00004", "to": "<SENDER>", "doc_id": DOC,
                "subject": "Re: Sales Representative", "body": "Dear <PERSON_1>,\n\nThank you for your email."},
    "source_hashes": [H1], "external_id": "AIA-sender0001",
}
STATUS = {
    "kind": "applicant_status", "agent": "recruitment", "subject": "Job Applicant/cyrine.jlassi.sr2@example.com",
    "reference_doctype": "Job Applicant", "reference_name": "cyrine.jlassi.sr2@example.com",
    "payload": {"applicant": "cyrine.jlassi.sr2@example.com", "status": "Shortlisted",
                "reason": "Meets 4/5 required skills for the Sales Representative opening."},
    "source_hashes": [H1], "external_id": "AIA-status0001",
}
RANKING = {
    "kind": "ranking", "agent": "recruitment", "subject": "Job Opening/HR-OPN-2026-0004",
    "reference_doctype": "Job Opening", "reference_name": "HR-OPN-2026-0004",
    "payload": {"opening": "HR-OPN-2026-0004", "note": "", "quarantined": ["mal.actor@example.com"],
                "entries": [
                    {"applicant": "cyrine.jlassi.sr2@example.com", "rank": 1, "score": 82.5,
                     "justification": "4/5 required skills (crm_tools, negotiation); 3 years of experience.",
                     "evidence": [{"source_hash": H1, "quote": "CRM: HubSpot"}]},
                    {"applicant": "salma.man@example.com", "rank": 2, "score": 61,
                     "justification": "3/5 required skills (negotiation); 2 years of experience.",
                     "evidence": [{"source_hash": H2, "quote": "Negotiation"}]}]},
    "source_hashes": [H1, H2], "external_id": "AIA-rank0001",
}
DIGEST = {
    "kind": "digest", "agent": "comms", "subject": "inbox/digest",
    "payload": {"items": [{"email_id": "IN-2026-00004", "priority": "high", "category": "candidate",
                           "summary": "<SENDER> asks about the Sales Representative opening."}]},
    "source_hashes": [H1], "external_id": "AIA-digest0001",
}


@pytest.fixture
def inbox(frappe):
    frappe.docs[("Inbound Email", "IN-2026-00004")] = {
        "sender_email": "cyrine.jlassi.sr2@example.com", "doc_id": DOC}
    return frappe


def test_sender_reply_reads_the_address_from_the_inbox_email(svc, adapter, inbox):
    rec, tr = submit(svc, adapter, SENDER_REPLY)
    assert rec.status is Status.PROPOSED, rec.violations
    assert tr.action.payload["recipients"] == [{"doctype": "Job Applicant", "name": "cyrine.jlassi.sr2@example.com"}]
    assert tr.action.subject.doctype == "Job Applicant"   # the person, not the Inbound Email


def test_sender_reply_to_an_unknown_sender_is_blocked(svc, adapter, inbox):
    inbox.docs[("Inbound Email", "IN-2026-00004")]["sender_email"] = "stranger@evil-mail.test"
    rec, _ = submit(svc, adapter, SENDER_REPLY)
    assert rec.status is Status.BLOCKED and "recipient_unknown" in rules(rec)


def test_sender_reply_for_a_missing_email_is_blocked(svc, adapter, frappe):
    rec, _ = submit(svc, adapter, SENDER_REPLY)
    assert rec.status is Status.BLOCKED and "recipient_unknown" in rules(rec)


def test_sender_reply_with_another_documents_id_is_blocked(svc, adapter, inbox):
    rec, _ = submit(svc, adapter, mutate(SENDER_REPLY, payload={"doc_id": "sha256:" + H2}))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


def test_sender_reply_without_doc_id_is_blocked(svc, adapter, inbox):
    record = copy.deepcopy(SENDER_REPLY)
    del record["payload"]["doc_id"]
    rec, _ = submit(svc, adapter, record)
    assert rec.status is Status.BLOCKED and "agent_output_malformed" in rules(rec)


def test_applicant_status_is_allowed(svc, adapter):
    rec, tr = submit(svc, adapter, STATUS)
    assert rec.status is Status.PROPOSED, rec.violations
    assert tr.action.tool == "update_applicant_status"
    assert tr.action.payload == {"status": "Shortlisted", "reason": STATUS["payload"]["reason"]}


def test_applicant_status_for_another_applicant_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(STATUS, payload={"applicant": "imen.baccouche.dm7@example.com"}))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


def test_applicant_status_with_protected_reason_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(STATUS, payload={"reason": "Rejected: she is too old for the team."}))
    assert rec.status is Status.BLOCKED and "protected_term_in_text" in rules(rec)


def test_ranking_list_is_allowed(svc, adapter):
    rec, tr = submit(svc, adapter, RANKING)
    assert rec.status is Status.PROPOSED, rec.violations   # "salma.man@" is not a protected term
    assert (tr.action.tool, tr.action.subject.doctype) == ("rank_shortlist", "Job Opening")
    assert [e["rank"] for e in tr.action.payload["entries"]] == [1, 2]


@pytest.mark.parametrize("entry, rule", [
    ({"justification": ""}, "justification_missing"),
    ({"justification": "good"}, "justification_too_short"),
    ({"score": 140}, "score_out_of_range"),
    ({"justification": "Strong profile, and young enough to grow with us."}, "protected_term_in_text"),
])
def test_bad_ranking_entry_is_blocked(svc, adapter, entry, rule):
    record = copy.deepcopy(RANKING)
    record["payload"]["entries"][1].update(entry)
    rec, _ = submit(svc, adapter, record)
    assert rec.status is Status.BLOCKED and rule in rules(rec)


def test_ranking_for_another_opening_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(RANKING, reference_name="HR-OPN-2026-0001"))
    assert rec.status is Status.BLOCKED and "subject_mismatch" in rules(rec)


def test_ranking_by_comms_agent_is_blocked(svc, adapter):
    rec, _ = submit(svc, adapter, mutate(RANKING, agent="comms"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)


def test_digest_is_allowed_for_comms_only(svc, adapter):
    rec, _ = submit(svc, adapter, DIGEST)
    assert rec.status is Status.PROPOSED, rec.violations
    rec, _ = submit(svc, adapter, mutate(DIGEST, agent="recruitment", external_id="AIA-digest0002"))
    assert rec.status is Status.BLOCKED and "tool_not_allowed" in rules(rec)
