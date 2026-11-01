"""S01: a candidate emails HR -> security layer -> Comms agent drafts a masked reply -> HR approves ->
Execution Gateway puts the real name back and sends it (Mailpit)."""

import smtplib
import time
import uuid
from email.message import EmailMessage

import rhflow as r

r.require_stack("hr_core", "ingest", "guardrails", "agents", "mailpit", "ollama")
adm = r.admin()
tag = time.strftime("%H%M%S")
SENDER, NAME = "dorra.mansouri.da5@example.com", "Dorra Mansouri"   # fictional Data Analyst applicant
subject = f"Question about the Data Analyst position [{tag}]"

r.step("1. A candidate writes to the HR inbox")
msg = EmailMessage()
msg["From"], msg["To"], msg["Subject"] = SENDER, "hr@rhflow.local", subject
msg["Message-ID"] = f"<s01-{uuid.uuid4().hex[:12]}@example.com>"
msg.set_content("Hello,\nI applied for the Data Analyst role last week. Could you tell me about the next steps "
                "and the expected timeline? My phone is +216 22 345 678.\nThank you,\nDorra Mansouri")
with smtplib.SMTP("localhost", 1025, timeout=15) as smtp:
    smtp.send_message(msg)
r.info(f"sent '{subject}' from {SENDER}")

r.step("2. HR Flow pulls the inbox through the input security layer (Sync inbox & CVs)")
res = adm.method("hr_ai.ingest.security_sync")
r.check(res.status_code == 200, "security_sync accepted", str(res.json().get("message", {}).get("inbox")))
rows = r.wait(lambda: adm.list("Inbound Email", ["name", "verdict", "released_to_ai", "subject", "agent_event"],
                               {"subject": subject}), timeout=90)
inbound = rows[0] if rows else {}
r.check(bool(inbound), "Inbound Email record created", inbound.get("name", "-"))
r.check(inbound.get("verdict") == "clean" and inbound.get("released_to_ai") == 1,
        "verdict clean, released to the AI", f"verdict={inbound.get('verdict')}")

r.step("3. Signed webhook -> Comms agent drafts a reply (local qwen3:14b, 30-120 s)")
act = r.wait(lambda: adm.latest_action(subject=f"email/{inbound.get('name')}"), timeout=420, every=5)
r.check(bool(act), "Comms draft proposed as an AI Action", (act or {}).get("name", "timeout"))
if not act:
    r.done()
a = adm.action(act["name"])
payload = a["payload"]
r.info("summary as HR sees it:\n      " + a["summary"].replace("\n", "\n      "))
r.check(a["status"] == "Proposed" and a["agent"] == "comms", "status Proposed, agent comms")
r.check("<SENDER>" in payload, "recipient is the <SENDER> placeholder (model never sees the address)")
r.check(NAME.split()[0] not in payload and "22 345 678" not in payload,
        "no real name / phone in the draft (PII masked before the LLM)")
r.check(len(a.get("action_hash") or "") == 64 and a.get("source_hashes"), "action hash + source hash recorded")
r.check(not r.mailpit_messages(f'subject:"{subject}" to:hr.review@rhflow.local'), "nothing sent before approval")

r.step("4. HR clicks Allow -> signed approval token -> Execution Gateway")
ap = adm.approve(a["name"])
r.check(ap.status_code == 200, "approve() accepted", str(ap.status_code))
a = adm.wait_action(a["name"])
r.check(a["status"] == "Executed", "status Executed", a.get("execution_result", "")[:120])

r.step("5. The sent email carries the real name (de-tokenised by the gateway only)")
sent = r.wait(lambda: r.mailpit_messages(f"to:hr.review@rhflow.local subject:\"{tag}\""), timeout=30)
body = r.mailpit_body(sent[0]["ID"]) if sent else ""
r.check(bool(sent), "reply delivered to the review inbox (test mode)")
r.check(NAME.split()[0] in body or SENDER in body, "real name / intended recipient restored in the sent mail")
r.check("<PERSON_" not in body and "<SENDER>" not in body, "no placeholder left in the sent mail")

r.step("6. Idempotency: approving / executing again does nothing")
again = adm.approve(a["name"])
r.check(again.status_code != 200, "second approval refused", r.server_messages(again)[0][:90])
time.sleep(3)
r.check(len(r.mailpit_messages(f"to:hr.review@rhflow.local subject:\"{tag}\"")) == 1, "exactly one email sent")

r.step("7. Audit")
v = r.guardrails_get("/audit/verify", adm).json()
r.check(v.get("ok") is True, "guardrails audit hash chain intact", f"{v.get('entries')} entries")
r.done()
