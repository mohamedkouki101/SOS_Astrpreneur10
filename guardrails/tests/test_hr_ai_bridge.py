"""HR Flow (hr_ai) asking the guardrails, end to end.

hr_ai/hr_ai/guardrails_client.py is loaded as is and its HTTP call is routed to this
service's real FastAPI app: the records below have the shape of AI Action records as
HR Flow stores them (JSON text fields, extra Frappe fields). All data is fictional."""
import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

from test_api import make_client

CLIENT_FILE = Path(__file__).resolve().parents[2] / "hr_ai" / "hr_ai" / "guardrails_client.py"
H1 = "bcee57a58f42ad9bbd3c92520295b83c546b241fb23109042b3f5957225869b5"
DOC = "sha256:" + H1
CONF = {"hr_ai_guardrails_url": "http://guardrails:8100/", "hr_ai_guardrails_api_key": "hr",
        "hr_ai_guardrails_api_secret": "secret"}


class RequestException(Exception):
    pass


@pytest.fixture
def client(tmp_path):
    c = make_client(tmp_path)
    c.frappe.docs[("Inbound Email", "IN-2026-00004")] = {
        "sender_email": "cyrine.jlassi.sr2@example.com", "doc_id": DOC}
    yield c
    c.db.close()


@pytest.fixture
def bridge(client, monkeypatch):
    """hr_ai's client module, with requests.post forwarded to the guardrails app."""
    fake = types.ModuleType("requests")
    fake.RequestException = RequestException
    fake.calls = []

    def post(url, json=None, headers=None, timeout=None):
        fake.calls.append(url)
        return client.post(url.split("8100", 1)[1], json=json, headers=headers)

    fake.post = post
    monkeypatch.setitem(sys.modules, "requests", fake)
    spec = importlib.util.spec_from_file_location("hr_ai_guardrails_client", CLIENT_FILE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.fake_requests = fake
    return module


def ai_action(**changes):
    """An AI Action record as hr_ai stores it (payload etc. as JSON text)."""
    record = {
        "doctype": "AI Action", "name": "AIA-2026-00009", "status": "Approved", "approver": "hr@rhflow.local",
        "kind": "email_reply", "agent": "comms", "subject": "email/IN-2026-00004",
        "reference_doctype": "Inbound Email", "reference_name": "IN-2026-00004",
        "summary": "To: <SENDER>\nSubject: Re: Sales Representative\n\nDear <PERSON_1>, ...",
        "payload": json.dumps({"email_id": "IN-2026-00004", "to": "<SENDER>", "doc_id": DOC,
                               "subject": "Re: Sales Representative",
                               "body": "Dear <PERSON_1>,\n\nThank you for your email."}),
        "source_hashes": json.dumps([H1]), "token_map": None,
        "action_hash": "0" * 64, "external_id": "AIA-6ec528d223", "run_id": "",
    }
    record.update(changes)
    return record


def test_reply_to_the_inbox_sender_is_allowed(bridge):
    verdict = bridge.check(ai_action(), CONF)
    assert verdict["allowed"] is True
    assert verdict["translated"]["recipients"] == [{"doctype": "Job Applicant",
                                                    "name": "cyrine.jlassi.sr2@example.com"}]
    assert bridge.fake_requests.calls == ["http://guardrails:8100/agent-actions/check"]


def test_injected_recipient_is_blocked(bridge):
    payload = {"email_id": "IN-2026-00004", "to": "[EMAIL_1]", "subject": "Your bank details",
               "body": "Please confirm your bank details."}
    record = ai_action(payload=json.dumps(payload), token_map=json.dumps({"[EMAIL_1]": "attacker@evil-mail.test"}))
    with pytest.raises(bridge.GuardrailsRefused, match="Blocked by guardrails: .*recipient_unknown"):
        bridge.check(record, CONF)


def test_kind_the_agent_may_not_use_is_blocked(bridge):
    with pytest.raises(bridge.GuardrailsRefused, match="tool_not_allowed"):
        bridge.check(ai_action(agent="recruitment"), CONF)


def test_ranking_record_is_allowed(bridge):
    payload = {"opening": "HR-OPN-2026-0004", "note": "", "quarantined": [], "entries": [
        {"applicant": "cyrine.jlassi.sr2@example.com", "rank": 1, "score": 82.5,
         "justification": "4/5 required skills (crm_tools, negotiation); 3 years of experience.",
         "evidence": [{"source_hash": H1, "quote": "CRM: HubSpot"}]}]}
    record = ai_action(kind="ranking", agent="recruitment", subject="Job Opening/HR-OPN-2026-0004",
                       reference_doctype="Job Opening", reference_name="HR-OPN-2026-0004",
                       payload=json.dumps(payload))
    assert bridge.check(record, CONF)["allowed"] is True


def test_not_configured_fails_closed(bridge):
    with pytest.raises(bridge.GuardrailsRefused, match="not configured"):
        bridge.check(ai_action(), {**CONF, "hr_ai_guardrails_api_secret": ""})
    assert bridge.fake_requests.calls == []


def test_unreachable_fails_closed(bridge):
    def down(*args, **kwargs):
        raise RequestException("connection refused")

    bridge.fake_requests.post = down
    with pytest.raises(bridge.GuardrailsRefused, match="no verdict .no answer."):
        bridge.check(ai_action(), CONF)


def test_bad_credentials_fail_closed(bridge):
    with pytest.raises(bridge.GuardrailsRefused, match="HTTP 401"):
        bridge.check(ai_action(), {**CONF, "hr_ai_guardrails_api_key": "nobody"})


def test_agent_account_cannot_be_used_as_the_hr_account(bridge):
    # hr_ai must call with an HR account; an agent account proposing as another agent is refused.
    with pytest.raises(bridge.GuardrailsRefused, match="HTTP 403"):
        bridge.check(ai_action(), {**CONF, "hr_ai_guardrails_api_key": "rh_recruit"})


def test_only_ai_action_fields_are_sent(bridge):
    body = bridge.check_body(ai_action())
    assert set(body) <= set(bridge.CHECK_FIELDS)            # no doctype / status / approver
    assert isinstance(body["payload"], dict) and "token_map" not in body and "run_id" not in body
