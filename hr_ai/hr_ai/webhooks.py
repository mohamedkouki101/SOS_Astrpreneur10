"""Signed calls from HR Flow to the agent service (Frappe -> agents).

Every request carries three headers:
    X-RHFlow-Event-Id   idempotency key (one event = one agent run, however often it is delivered)
    X-RHFlow-Timestamp  unix seconds; the agent service refuses anything outside its replay window
    X-RHFlow-Signature  "sha256=" + HMAC-SHA256(secret, f"{timestamp}.{event_id}.".encode() + body)
The body never contains document text: only record names and input-layer report ids. The agents
fetch the masked, signed envelope from the layer themselves.

Site config (bench set-config, never in git): hr_ai_agents_url, hr_ai_agents_webhook_secret.
The same secret is WEBHOOK_HMAC_SECRET in the agent service's .env.
"""

import hashlib
import hmac
import json
import time

import frappe
import requests


class AgentsError(Exception):
    pass


def sign(secret: str, timestamp: str, event_id: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), f"{timestamp}.{event_id}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def call_agents(path: str, event: dict, timeout: int = 30) -> dict:
    url, secret = frappe.conf.get("hr_ai_agents_url"), frappe.conf.get("hr_ai_agents_webhook_secret")
    if not (url and secret):
        raise AgentsError("site config hr_ai_agents_url / hr_ai_agents_webhook_secret is not set")
    body = json.dumps(event, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-RHFlow-Event-Id": event["event_id"],
               "X-RHFlow-Timestamp": ts, "X-RHFlow-Signature": sign(secret, ts, event["event_id"], body)}
    try:
        r = requests.post(url.rstrip("/") + path, data=body, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise AgentsError(f"agent service unreachable ({type(exc).__name__})") from None
    if r.status_code >= 400:
        raise AgentsError(f"agent service {path}: HTTP {r.status_code}")
    return r.json()


# ---------------------------------------------------------------- HR inbox -> comms agent


def notify_inbound_email(name: str):
    """Queue the event after the Inbound Email is committed (called by the inbox sync and by HR release)."""
    frappe.enqueue("hr_ai.webhooks.send_inbound_email", queue="short", name=name, enqueue_after_commit=True)


def send_inbound_email(name: str) -> str:
    rec = frappe.db.get_value("Inbound Email", name, ["released_to_ai", "report_id"], as_dict=True)
    if not rec or not rec.released_to_ai or not rec.report_id:
        return "skipped"  # never send held-back mail to the agents
    try:
        call_agents("/events", {"event_id": f"inbound_email:{name}:{rec.report_id[7:23]}",
                                "type": "inbound_email.received", "name": name})
        state = "sent"
    except AgentsError:
        frappe.log_error(title=f"Agent event for {name} failed")
        state = "failed"
    frappe.db.set_value("Inbound Email", name, "agent_event", state, update_modified=False)
    frappe.db.commit()
    return state


def retry_failed_events(limit: int = 5):
    """Scheduler: deliveries that failed (agent service down) are retried; the receiver is idempotent."""
    for name in frappe.get_all("Inbound Email", filters={"agent_event": "failed", "released_to_ai": 1},
                               pluck="name", order_by="received_on asc", limit=limit):
        send_inbound_email(name)
