# 01 · Candidate email → masked draft → HR approval → sent with the real name

**Story.** Dorra Mansouri (fictional Data Analyst applicant) emails the HR inbox to ask about the next
steps. Without anyone touching it, HR Flow pulls the mail through the security layer, the Comms agent drafts
a reply in which it never sees her name, address or phone number, and the draft waits for HR. HR clicks
**Allow**; only then does the Execution Gateway put the real name back and send the email.

Script: `scripts/s01_inbox_reply.py` · about 2 minutes (one model run)

## Layers exercised
| Step | Layer |
|---|---|
| Mail arrives in Mailpit, inbox sync every minute / **Sync inbox & CVs** | Integration: scheduler |
| `/v1/email`: sanitise → mask PII (`<PERSON_1>`, phone) → injection scan → provenance hash | Input security layer, stages 1-4 |
| Inbound Email record, sender stored in an HR-only field | HR Core, field-level permissions |
| HMAC-signed `inbound_email.received` event, idempotent | Integration: signed webhook |
| Comms agent reads the signed, masked envelope; `propose_email_reply` with `<SENDER>` | Agent layer + output validator |
| `propose_action` → guardrails `/agent-actions/check` (send_email allowed, recipient = original sender) | Guardrails: policy |
| **Allow** → approval token = HMAC(name, action hash, approver) | Human approval |
| Gateway: re-check hash + token + guardrails, resolve `<PERSON_1>` (RESOLVER key) and `<SENDER>`, send | Execution Gateway |
| Second approval refused, one email only | Idempotency |
| `/audit/verify` | Audit log |

## Show it live
1. Send the email: `$P -m agents.scripts.demo_scenario --reset` (or let the script send a fresh one).
2. RH AI → **Sync inbox & CVs**. The Inbound Email appears with verdict `clean`.
3. Wait 30-120 s: a **Comms** card "Re: Question about the Data Analyst position" arrives in *Waiting for decision*.
4. **Details / edit → Payload**: `"to": "<SENDER>"`, body `Hello <PERSON_1>,`. No name, no phone number.
   Integrity section: action hash and the source hash of the email.
5. **Allow** → confirm. Status goes Approved, then Executed (a few seconds).
6. Mailpit (http://localhost:8025): new mail to `hr.review@rhflow.local`, banner "Intended recipient:
   dorra.mansouri.da5@example.com", body "Hello Dorra Mansouri,".

## Expected
- Draft is `Proposed`, agent `comms`, recipient `<SENDER>`, no real name or phone number in the payload.
- Nothing in Mailpit before approval; exactly one email after.
- Execution result `Email sent to hr.review@rhflow.local (test mode; intended for …)`.
- Approving again: "Only Proposed actions can be decided (current status: Executed)".

## Last run (2026-11-01): 17/17 PASS
```
Inbound Email record created (IN-2026-00012) · verdict clean, released to the AI
Comms draft proposed as an AI Action (AIA-2026-00033)
recipient is the <SENDER> placeholder · no real name / phone in the draft
status Executed (Email sent to hr.review@rhflow.local (test mode; intended for dorra.mansouri.da5@example.com))
sent mail body: "Hello Dorra Mansouri, Thank you for your interest in the Data Analyst role…"
second approval refused · exactly one email sent · guardrails audit hash chain intact
```

## Notes
- The RH AI card text hides `<SENDER>` / `<PERSON_1>` (README finding 1); show the payload instead.
- Test mode: every outgoing mail goes to `hr_ai_test_recipient`, never to the fictional address.
