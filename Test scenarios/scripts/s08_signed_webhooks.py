"""S08: the trust boundary between HR Core and the agent service. Every call is HMAC-signed,
time-boxed (replay window) and idempotent (one event id = one agent run)."""

import json
import sqlite3
import sys
import time

import httpx
import rhflow as r

sys.path.insert(0, str(r.REPO))
from agents.integration.signing import sign  # same code as the receiver and hr_ai/webhooks.py  # noqa: E402

r.require_stack("agents")
secret = r.env()["WEBHOOK_HMAC_SECRET"]
URL = f"{r.AGENTS}/events"
ALERTS = r.REPO / "var/alerts.jsonl"


def signed(event: dict, ts: int | None = None, key: str = secret) -> tuple[bytes, dict]:
    body = json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ts = str(ts if ts is not None else int(time.time()))
    return body, {"Content-Type": "application/json", "X-RHFlow-Event-Id": event["event_id"],
                  "X-RHFlow-Timestamp": ts, "X-RHFlow-Signature": sign(key, ts, event["event_id"], body)}


def post(body: bytes, headers: dict) -> httpx.Response:
    return httpx.post(URL, content=body, headers=headers, timeout=15)


alerts_before = len(ALERTS.read_text().splitlines()) if ALERTS.exists() else 0
new_event = {"event_id": f"s08:{int(time.time())}", "type": "inbound_email.received", "name": "IN-2026-99999"}

r.step("1. No signature / wrong key / tampered body -> 401, nothing runs")
res = httpx.post(URL, json=new_event, timeout=15)
r.check(res.status_code == 401, "unsigned request rejected", str(res.status_code))
body, h = signed(new_event, key="not-the-shared-secret")
r.check(post(body, h).status_code == 401, "request signed with the wrong key rejected")
body, h = signed(new_event)
tampered = body.replace(b"IN-2026-99999", b"IN-2026-00001")
r.check(post(tampered, h).status_code == 401, "body changed after signing rejected")
body, h = signed({**new_event, "event_id": "other-id"})
h["X-RHFlow-Event-Id"] = new_event["event_id"]
r.check(post(body, h).status_code == 401, "header / body event id mismatch rejected")

r.step("2. Replay window: a correctly signed but old request -> 401")
body, h = signed(new_event, ts=int(time.time()) - 3600)
r.check(post(body, h).status_code == 401, "1-hour-old signed request rejected")

r.step("3. Idempotency: an event that already ran is never run twice")
db = sqlite3.connect(r.REPO / "var/events.sqlite")
row = db.execute("SELECT event_id, result FROM events WHERE status='done' AND type='inbound_email.received' "
                 "ORDER BY updated DESC LIMIT 1").fetchone()
if row:
    event_id, result = row
    name = event_id.split(":")[1]
    old = {"event_id": event_id, "type": "inbound_email.received", "name": name}
    first = post(*signed(old))
    second = post(*signed(old))   # same event, re-signed (a retry), and the exact same bytes again
    r.check(first.status_code == 200 and first.json().get("status") == "duplicate",
            f"re-delivery of {event_id} answered 'duplicate'", first.text[:80])
    r.check(second.status_code == 200 and second.json().get("status") == "duplicate", "second re-delivery: still duplicate")
    proposals = json.loads(result).get("proposals")
    r.info(f"the original run produced {proposals}; no new agent run, no new proposal")
else:
    r.check(False, "no finished event yet: run S01 first")

r.step("4. Every rejection is an alert for the Monitor")
lines = ALERTS.read_text().splitlines()[alerts_before:] if ALERTS.exists() else []
rejected = [json.loads(x) for x in lines if '"webhook_rejected"' in x]
for a in rejected:
    r.info(f"alert webhook_rejected: {a.get('reason')}")
r.check(len(rejected) >= 5, "5 webhook_rejected alerts written", str(len(rejected)))
r.done()
