import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from guardrails.agent_layer import AgentLayerAdapter, FrappeDirectory
from guardrails.api import ApiConfig, Services, create_app
from guardrails.approval import ApprovalService, ApprovalSigner
from guardrails.auth import AuthError, Identity, credential_from
from guardrails.fairness import FairnessChecker
from guardrails.gateway import ExecutionGateway
from guardrails.monitor import Monitor
from guardrails.policy import PolicyEngine
from guardrails.storage import AuditLog, Database, SQLiteActionStore

from test_gateway import FakeFrappe

CFG = Path(__file__).resolve().parents[1] / "config"
KEY = b"test-key-0123456789-0123456789-0123456789"
SRC = "sha256:" + "a" * 64

IDENTITIES = {
    "comms": Identity("svc.comms@hr-ai.test", frozenset({"AI Agent"})),
    "recruit": Identity("svc.recruitment@hr-ai.test", frozenset({"AI Agent"})),
    "ingest": Identity("svc.ingest@hr-ai.test", frozenset({"Ingest"})),
    "hr": Identity("hr.manager@acme.test", frozenset({"HR Manager", "Employee"})),
    "employee": Identity("employee@acme.test", frozenset({"Employee"})),
    # the team's real agent-layer accounts and roles
    "rh_comms": Identity("agent.comms@rhflow.local", frozenset({"RH Flow Agent Comms"})),
    "rh_recruit": Identity("agent.recruitment@rhflow.local", frozenset({"RH Flow Agent Recruitment"})),
}


class FakeAuth:
    """Maps 'token <who>:secret' to an identity, using the real credential parsing."""

    def authenticate(self, method, headers, cookies):
        cred = credential_from(method, headers, cookies)
        who = cred.get("Authorization", "token :")[6:].split(":")[0]
        if who not in IDENTITIES:
            raise AuthError(401, "unauthenticated", "unknown token")
        return IDENTITIES[who]


def H(who):
    return {"Authorization": f"token {who}:secret"}


def make_client(tmp_path, **api_overrides):
    db = Database(tmp_path / "api.db")
    audit = AuditLog(db)
    fairness = FairnessChecker.from_file(CFG / "fairness.json")
    approvals = ApprovalService.from_file(
        CFG / "approval.json", policy=PolicyEngine.from_file(CFG / "policy.json"),
        fairness=fairness, signer=ApprovalSigner({"k1": KEY}, "k1"),
        store=SQLiteActionStore(db), audit=audit)
    frappe = FakeFrappe()
    api_cfg = json.loads((CFG / "api.json").read_text(encoding="utf-8"))
    api_cfg.update(api_overrides)
    services = Services(
        approvals=approvals,
        gateway=ExecutionGateway(approvals, frappe),
        monitor=Monitor.from_file(CFG / "monitor.json", audit, fairness=fairness),
        authenticator=FakeAuth(),
        config=ApiConfig.parse(api_cfg),
        job_criteria={"HR-OPN-0001": ("python_experience", "sql")},
        agent_layer=AgentLayerAdapter(json.loads((CFG / "agent_layer.json").read_text(encoding="utf-8")),
                                      FrappeDirectory(frappe)),
    )
    client = TestClient(create_app(services))
    client.frappe, client.db, client.services = frappe, db, services
    return client


@pytest.fixture
def client(tmp_path):
    c = make_client(tmp_path)
    yield c
    c.db.close()


def email_body(agent="comms_agent", recipients=None, action_id="a-1", **extra):
    return {
        "action_id": action_id, "agent": agent, "tool": "send_email",
        "subject": {"doctype": "Job Applicant", "name": "HR-APP-0001"},
        "payload": {"recipients": recipients or [{"doctype": "Job Applicant", "name": "HR-APP-0001"}],
                    "subject": "Your application", "body": "Thank you for applying."},
        "source_hashes": [SRC], "model": "claude-sonnet-5-5", "prompt_version": "comms-v1", **extra,
    }


def rank_body(criterion="python_experience", job="HR-OPN-0001"):
    return {
        "action_id": "r-1", "agent": "recruitment_agent", "tool": "rank_candidate",
        "subject": {"doctype": "Job Applicant", "name": "HR-APP-0001"},
        "payload": {"job_opening": job, "score": 80, "criteria": [{
            "criterion": criterion, "score": 80,
            "justification": "Five years of Django REST API development in production.",
            "source_hash": SRC}]},
        "source_hashes": [SRC],
    }


def alerts(client, **params):
    return client.get("/alerts", headers=H("hr"), params=params).json()


# --- basics -----------------------------------------------------------------------

def test_health_is_public(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_security_headers(client):
    r = client.get("/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["cache-control"] == "no-store"


def test_unauthenticated_refused(client):
    assert client.post("/actions", json=email_body()).status_code == 401
    assert client.get("/actions").status_code == 401


def test_me_shows_capabilities(client):
    assert client.get("/me", headers=H("hr")).json()["capabilities"] == ["hr"]
    assert client.get("/me", headers=H("comms")).json()["capabilities"] == ["agent:comms_agent"]


# --- the full happy path ------------------------------------------------------------

def test_propose_review_approve_execute(client):
    r = client.post("/actions", json=email_body(), headers=H("comms"))
    assert r.status_code == 201 and r.json()["status"] == "Proposed"

    view = client.get("/actions/a-1", headers=H("hr")).json()
    assert view["payload"]["body"] == "Thank you for applying."
    assert "approval_token" not in view
    assert [e["event"] for e in view["audit"]] == ["action.submitted"]

    r = client.post("/actions/a-1/approve", json={"seen_hash": view["action_hash"]}, headers=H("hr"))
    assert r.status_code == 200
    token = r.json()["approval_token"]

    r = client.post("/actions/a-1/execute", json={"token": token}, headers=H("hr"))
    assert r.status_code == 200 and r.json()["status"] == "Executed"
    assert client.frappe.writes()[0][2]["recipients"] == "amira.test@example.com"

    again = client.post("/actions/a-1/execute", json={"token": token}, headers=H("hr")).json()
    assert again["already_executed"] is True and len(client.frappe.writes()) == 1


def test_queue_lists_pending(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    items = client.get("/actions", params={"status": "Proposed"}, headers=H("hr")).json()
    assert [i["action_id"] for i in items] == ["a-1"]


# --- the demo: prompt injection blocked and reported ---------------------------------

def test_injected_recipient_blocked_and_alerted(client):
    r = client.post("/actions", json=email_body(recipients=["attacker@evil.com"]), headers=H("comms"))
    assert r.status_code == 201 and r.json()["status"] == "Blocked"
    assert r.json()["violations"][0]["rule"] == "recipient_not_a_record"
    got = alerts(client)
    assert [a["rule"] for a in got] == ["suspected_injection"]
    assert SRC in got[0]["message"]


# --- identity attacks -------------------------------------------------------------

def test_agent_cannot_impersonate_another_agent(client):
    r = client.post("/actions", json=rank_body() | {"agent": "recruitment_agent"}, headers=H("comms"))
    assert r.status_code == 403 and r.json()["detail"]["error"] == "agent_impersonation"
    assert ("instant", "critical") in [(a["rule"], a["severity"]) for a in alerts(client)]


def test_humans_cannot_propose(client):
    r = client.post("/actions", json=email_body(), headers=H("hr"))
    assert r.status_code == 403 and r.json()["detail"]["error"] == "forbidden_endpoint"


def test_agent_cannot_call_hr_endpoints(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    h = client.get("/actions/a-1", headers=H("hr")).json()["action_hash"]
    r = client.post("/actions/a-1/approve", json={"seen_hash": h}, headers=H("comms"))
    assert r.status_code == 403
    assert any(a["rule"] == "instant" and "forbidden_endpoint" in a["message"] for a in alerts(client))


def test_employee_cannot_see_queue(client):
    assert client.get("/actions", headers=H("employee")).status_code == 403


# --- approval rules over HTTP ------------------------------------------------------

def test_stale_hash_conflict(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    r = client.post("/actions/a-1/approve", json={"seen_hash": "sha256:" + "b" * 64}, headers=H("hr"))
    assert r.status_code == 409 and r.json()["detail"]["error"] == "stale_hash"


def test_edit_voids_token(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    h = client.get("/actions/a-1", headers=H("hr")).json()["action_hash"]
    token = client.post("/actions/a-1/approve", json={"seen_hash": h}, headers=H("hr")).json()["approval_token"]
    new_payload = {"recipients": [{"doctype": "Job Applicant", "name": "HR-APP-0001"}],
                   "subject": "Your application", "body": "Reworded by HR."}
    r = client.put("/actions/a-1/payload", json={"payload": new_payload}, headers=H("hr"))
    assert r.status_code == 200 and r.json()["status"] == "Proposed"
    assert client.post("/actions/a-1/execute", json={"token": token}, headers=H("hr")).status_code in (403, 409)
    assert client.frappe.writes() == []


def test_reject(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    r = client.post("/actions/a-1/reject", json={"reason": "Wrong tone"}, headers=H("hr"))
    assert r.status_code == 200 and r.json()["status"] == "Rejected"


def test_forged_token_refused(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    r = client.post("/actions/a-1/execute", json={"token": "abc.def"}, headers=H("hr"))
    assert r.status_code in (403, 409) and client.frappe.writes() == []


# --- trusted job criteria ----------------------------------------------------------

def test_ranking_on_listed_criterion_accepted(client):
    r = client.post("/actions", json=rank_body(), headers=H("recruit"))
    assert r.json()["status"] == "Proposed", r.json()


def test_ranking_on_unlisted_criterion_blocked(client):
    r = client.post("/actions", json=rank_body(criterion="cultural_fit"), headers=H("recruit"))
    assert r.json()["status"] == "Blocked"
    assert "criterion_not_job_related" in [v["rule"] for v in r.json()["violations"]]


def test_ranking_for_unknown_job_opening_blocked(client):
    r = client.post("/actions", json=rank_body(job="HR-OPN-9999"), headers=H("recruit"))
    assert r.json()["status"] == "Blocked"


def test_agent_cannot_send_its_own_criteria(client):
    body = rank_body(criterion="cultural_fit") | {"job_criteria": ["cultural_fit"]}
    assert client.post("/actions", json=body, headers=H("recruit")).status_code == 422


# --- input validation ---------------------------------------------------------------

def test_unknown_field_rejected(client):
    assert client.post("/actions", json=email_body(admin=True), headers=H("comms")).status_code == 422


def test_bad_source_hash_rejected(client):
    r = client.post("/actions", json=email_body(source_hashes=["not-a-hash"]), headers=H("comms"))
    assert r.status_code == 422


def test_bad_action_id_rejected(client):
    r = client.post("/actions", json=email_body(action_id="../../etc"), headers=H("comms"))
    assert r.status_code == 422


def test_duplicate_action_id_conflict(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    assert client.post("/actions", json=email_body(), headers=H("comms")).status_code == 409


def test_oversized_body_rejected(client):
    body = email_body()
    body["payload"]["body"] = "x" * 300_000
    assert client.post("/actions", json=body, headers=H("comms")).status_code == 413


def test_deeply_nested_payload_rejected(client):
    deep = {}
    cur = deep
    for _ in range(30):
        cur["x"] = {}
        cur = cur["x"]
    body = email_body()
    body["payload"]["extra"] = deep
    r = client.post("/actions", json=body, headers=H("comms"))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "payload_too_deep"


def test_rate_limit(tmp_path):
    c = make_client(tmp_path, rate_limit_per_minute=3)
    codes = [c.get("/me", headers=H("hr")).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    c.db.close()


# --- cookie auth + CSRF -------------------------------------------------------------

def test_cookie_post_without_csrf_header_refused():
    with pytest.raises(AuthError) as e:
        credential_from("POST", {}, {"sid": "abc"})
    assert e.value.code == "csrf"


def test_cookie_post_with_csrf_header_accepted():
    assert credential_from("POST", {"x-guardrails-csrf": "1"}, {"sid": "abc"}) == {"Cookie": "sid=abc"}


def test_cookie_get_accepted():
    assert credential_from("GET", {}, {"sid": "abc"}) == {"Cookie": "sid=abc"}


@pytest.mark.parametrize("header", ["Bearer abc", "token nocolon", "Basic dXNlcjpwYXNz"])
def test_malformed_authorization_refused(header):
    with pytest.raises(AuthError):
        credential_from("GET", {"authorization": header}, {})


def test_cors_blocks_unknown_origin(client):
    r = client.options("/actions", headers={"Origin": "https://evil.test",
                                             "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") != "https://evil.test"


def test_cors_allows_frappe_origin(client):
    r = client.options("/actions", headers={"Origin": "http://localhost:8080",
                                             "Access-Control-Request-Method": "POST"})
    assert r.headers.get("access-control-allow-origin") == "http://localhost:8080"


# --- monitor, audit, fairness endpoints -------------------------------------------------

def test_ack_alert(client):
    client.post("/actions", json=email_body(recipients=["attacker@evil.com"]), headers=H("comms"))
    alert_id = alerts(client)[0]["id"]
    assert client.post(f"/alerts/{alert_id}/ack", json={"note": "CV quarantined"}, headers=H("hr")).status_code == 200
    assert alerts(client) == []


def test_audit_verify_and_list(client):
    client.post("/actions", json=email_body(), headers=H("comms"))
    v = client.get("/audit/verify", headers=H("hr")).json()
    assert v["ok"] is True and v["entries"] >= 1
    entries = client.get("/audit", params={"action_id": "a-1"}, headers=H("hr")).json()
    assert entries[0]["event"] == "action.submitted"


def test_input_flag_from_ingest_only(client):
    body = {"source_hash": SRC, "detector": "rules", "rules": ["ignore_previous_instructions"]}
    assert client.post("/input-flags", json=body, headers=H("ingest")).status_code == 201
    assert client.post("/input-flags", json=body, headers=H("comms")).status_code == 403
    assert "suspected_injection" in [a["rule"] for a in alerts(client)]


def test_disparity_endpoint(client):
    outcomes = [{"group": "A", "selected": i < 6} for i in range(10)] + \
               [{"group": "B", "selected": i < 3} for i in range(10)]
    r = client.post("/fairness/disparity", json={"job_opening": "HR-OPN-0001", "outcomes": outcomes},
                    headers=H("hr")).json()
    assert r["status"] == "adverse_impact" and r["flagged"] == ["B"] and r["alerts"]


# --- agent layer format (hr_ai AI Action) ----------------------------------------------

def agent_email(**over):
    from test_agent_layer import EMAIL_REPLY, mutate
    return mutate(EMAIL_REPLY, **over)


def test_agent_layer_proposal_queued_for_hr(client):
    r = client.post("/agent-actions", json=agent_email(), headers=H("rh_comms"))
    assert r.status_code == 201, r.json()
    body = r.json()
    assert body["status"] == "Proposed" and body["action_id"] == "AIA-6ec528d223"
    assert body["translated"]["recipients"] == [{"doctype": "Job Applicant",
                                                 "name": "cyrine.jlassi.sr2@example.com"}]
    assert body["agent_action_hash"].startswith("84c22d")
    # then the normal HR flow works on it
    view = client.get("/actions/AIA-6ec528d223", headers=H("hr")).json()
    token = client.post("/actions/AIA-6ec528d223/approve", json={"seen_hash": view["action_hash"]},
                        headers=H("hr")).json()["approval_token"]
    ex = client.post("/actions/AIA-6ec528d223/execute", json={"token": token}, headers=H("hr")).json()
    assert ex["status"] == "Executed"
    assert client.frappe.writes()[0][2]["content"].startswith("Dear Cyrine Jlassi,")


def test_agent_layer_token_swap_blocked_and_alerted(client):
    r = client.post("/agent-actions", json=agent_email(token_map={"[EMAIL_1]": "attacker@evil-mail.test"}),
                    headers=H("rh_comms"))
    assert r.status_code == 201 and r.json()["status"] == "Blocked"
    assert "recipient_unknown" in [v["rule"] for v in r.json()["violations"]]
    assert "suspected_injection" in [a["rule"] for a in alerts(client)]


def test_agent_layer_impersonation_refused(client):
    from test_agent_layer import INTERVIEW
    r = client.post("/agent-actions", json=INTERVIEW, headers=H("rh_comms"))   # comms posing as recruitment
    assert r.status_code == 403 and r.json()["detail"]["error"] == "agent_impersonation"


def test_agent_layer_roles_cannot_approve(client):
    client.post("/agent-actions", json=agent_email(), headers=H("rh_comms"))
    r = client.post("/actions/AIA-6ec528d223/approve", json={"seen_hash": SRC}, headers=H("rh_comms"))
    assert r.status_code == 403


def test_agent_layer_malformed_is_422(client):
    r = client.post("/agent-actions", json=agent_email(approver="Administrator"), headers=H("rh_comms"))
    assert r.status_code == 422 and r.json()["detail"]["error"] == "agent_output_invalid"


def test_check_endpoint_is_a_dry_run(client):
    ok = client.post("/agent-actions/check", json=agent_email(), headers=H("hr")).json()
    bad = client.post("/agent-actions/check", json=agent_email(
        token_map={"[PERSON_1]": "Click http://evil.test/pay"}), headers=H("hr")).json()
    assert ok["allowed"] is True and ok["violations"] == []
    assert bad["allowed"] is False and bad["violations"][0]["rule"] == "token_value_not_from_record"
    assert client.get("/actions", headers=H("hr")).json() == []   # nothing stored


def test_check_endpoint_refuses_employees(client):
    assert client.post("/agent-actions/check", json=agent_email(), headers=H("employee")).status_code == 403


# --- config -------------------------------------------------------------------------

def test_wildcard_cors_refused():
    cfg = json.loads((CFG / "api.json").read_text(encoding="utf-8"))
    cfg["cors_origins"] = ["*"]
    with pytest.raises(ValueError):
        ApiConfig.parse(cfg)
