# Guardrails & Execution Control

Separate service, outside Frappe. Talks to Frappe **only through its REST API**.
Agents send *proposed actions*; this service decides, asks a human, and is the
only component allowed to write to Frappe.

> Team decision (2026-10-31): implemented as a standalone service instead of
> inside the `hr_ai` Frappe app (CLAUDE.md §9), for faster iteration.

**Integrating another layer (agents / `hr_ai`, ingest service, HR interface)?** See [INTEGRATION.md](INTEGRATION.md).

## Status
| Component | State |
|---|---|
| Policy Engine (default-deny) | done, `guardrails/policy.py` |
| Fairness checks | done, `guardrails/fairness.py` |
| Human approval (signed token bound to action hash) | done, `guardrails/approval.py`, `guardrails/canonical.py` |
| Execution Gateway (sole writer, idempotent) | done, `guardrails/gateway.py`, `guardrails/executors.py`, `guardrails/frappe_client.py` |
| Audit log (append-only, hash-chained) + SQLite persistence | done, `guardrails/storage.py` |
| Monitor | done, `guardrails/monitor.py` |
| HTTP API (FastAPI) | done, `guardrails/api.py`, `guardrails/auth.py`, `guardrails/settings.py` |

## Policy Engine
Rules live in `config/policy.json`. An action is allowed only if:
1. the agent is registered,
2. the tool is on that agent's allowlist,
3. the tool is defined,
4. all of the tool's constraints pass: subject doctype, recipient rules, rate limit.

Recipients must be Frappe record references (`{"doctype": "Job Applicant", "name": "HR-APP-0001"}`),
never raw email addresses. The gateway resolves the address from Frappe, so a
prompt-injected "send this to attacker@evil.com" cannot be expressed.

A typo or unknown key in the config makes the service refuse to start: a
misspelled limit must never silently disappear.

## Fairness checks
Rules live in `config/fairness.json`.
1. **Protected attributes blind** (every action): no key like `age`, `gender`,
   `dateOfBirth`, `nationality`, `photo`... anywhere in the payload, at any
   depth; no embedded images.
2. **Job-related justification** (`rank_candidate`): overall score plus at least one
   criterion, each `{criterion, score, justification, source_hash}`. The justification
   must be long enough, cite a source of the action, and the criterion must be
   one of the job opening's criteria. Free text must not mention protected traits
   (English + French word list, whole-word matching).
3. **Score-disparity test** (batch): four-fifths rule (EEOC). A group whose
   selection rate is < 80% of the best group's is flagged for the human
   reviewer. Groups under 5 candidates are not judged. Group labels come
   from fictional trusted data, never from the agent.

## Human approval
```
submit ──▶ Proposed ──approve──▶ Approved ──(gateway)──▶ Executed | Failed
   │          │  ▲                  │
   │          │  └──── edit ────────┘   any edit voids the approval
   │          └──reject──▶ Rejected
   └──▶ Blocked  (policy or fairness said no; cannot be approved)
```
- **Action hash**: SHA-256 of canonical JSON (sorted keys, UTF-8, RFC 8785 style)
  over action_id, agent, tool, subject, payload and source hashes.
- **Approval token**: HMAC-SHA256 over `{action_id, action_hash, approver, iat, exp, kid}`.
  Key from `GUARDRAILS_APPROVAL_KEY` (see `example.env`), >= 32 bytes, rotatable via key id.
- The approver sends the hash they **saw**; if the action changed meanwhile, approval is refused.
- Only users with an approver role (`config/approval.json`) can approve, reject or edit.
  Accounts with the `AI Agent` role, or named like an agent, never can.
- Editing re-runs policy and fairness checks and clears the token.
- `verify()` (used by the gateway) re-hashes the current content and checks
  signature, action, approver and expiry.

## Execution Gateway
The only component holding Frappe **write** credentials (`FRAPPE_GATEWAY_API_KEY/SECRET`).
`execute(action_id, token)`:
1. Already executed: return the stored result, write nothing (idempotent).
2. Verify approval: Approved, genuine token, hash = current content.
3. Re-check policy + fairness (rules or limits may have changed).
4. Atomic claim Approved -> Executing: concurrent calls cannot both run.
5. Run the tool executor over Frappe REST, record Executed or Failed.

| Tool | Frappe writes |
|---|---|
| `send_email`, `answer_employee` | `frappe.core.doctype.communication.email.make` (address looked up from the record) |
| `propose_interview_slots` | same, slots appended to the body |
| `rank_candidate` | `Job Applicant.applicant_rating` + a Comment with the justification |
| `update_applicant_status` | `Job Applicant.status` + a Comment with the reason |

- Payloads are re-validated with exact keys, so an extra `bcc` is refused.
- Agent text is HTML-escaped, so no injected links, pixels or scripts.
- **At most once:** if Frappe times out, the action becomes `Failed / outcome_unknown`
  and is never retried automatically; a human checks Frappe first.
- Field names to confirm on our v16 site: `email_id`, `job_title`, `applicant_rating`,
  `prefered_email`, `company_email`.

## Persistence & audit log
- **SQLite** file (`GUARDRAILS_DB_PATH`, gitignored) holds the action queue and the audit log.
  WAL mode, `synchronous=FULL`. The atomic `Approved -> Executing` claim is one
  conditional UPDATE, safe across threads and processes.
- A state change and its audit entry are written in **one transaction**: no
  approval or execution can exist without its log entry.
- **Audit log**: append-only and hash-chained. `entry_hash = SHA-256(seq, ts, event,
  actor, action_id, details, prev_hash)`, the first entry links to a zero "genesis" hash.
  - SQLite triggers refuse `UPDATE` / `DELETE` on `audit_log`.
  - Even if someone drops the triggers, `verify_chain()` finds the first modified,
    deleted or reordered entry.
  - `head()` gives the latest hash. Publish it elsewhere (demo slide, team chat) to
    also detect deletion of the newest entries.
- Events: `action.submitted` (incl. Blocked + rule codes), `action.approved`,
  `action.rejected`, `action.edited`, `approval.denied`, `execution.started`,
  `execution.succeeded`, `execution.failed`, `execution.refused`, `execution.duplicate`.
- Each entry records actor, agent, input (source) hashes, model and prompt version,
  policy/fairness versions, approver and result. **Never** logged: email bodies,
  candidate data, approval tokens, secrets.

## Suspicious-Action Monitor
Reads new audit entries (`run_once()`, cursor stored in the DB) and raises alerts.
It **observes only**: enforcement stays in the Policy Engine and the gateway.
Rules live in `config/monitor.json`.

| Rule | Triggers when | Default severity |
|---|---|---|
| `suspected_injection` | a proposal is Blocked for an exfiltration/bias rule, or the input pipeline reports a flag | high |
| `instant` | an agent tries to approve (`self_approval`), a forged or mismatched token, an integrity error | critical / high |
| `repeated_denials` | the same actor is denied 3 times within 10 min | high |
| `proposal_flood` | one agent proposes 50 actions within 5 min | warning |
| `bulk_send` | 20 emails executed within 10 min | high |
| `off_hours` | approve/edit/execute outside Mon–Fri 08:00–19:00 (UTC+1, Tunis) | warning |
| `execution_failure` | an execution failed; "outcome unknown" says to check Frappe first | high |
| `audit_chain_broken` | `verify_chain()` fails | critical |
| `adverse_impact` | `check_disparity()` fails the four-fifths rule for a batch | high |

- Alerts are stored in `monitor_alerts`, deduplicated, and written to the audit
  log as `monitor.alert`. HR acknowledges them (`acknowledge()`, also audited).
- Notifiers are pluggable (`ConsoleNotifier` for now). A failing notifier never
  stops monitoring. Alert channel still to decide (open question in CLAUDE.md).
- The input pipeline reports quarantined documents with `report_input_flag()`.

## HTTP API
Interactive docs: `http://localhost:8100/docs` once running.

**Authentication**: the caller sends its own Frappe credentials (`Authorization: token KEY:SECRET`,
or the Frappe `sid` cookie plus the `X-Guardrails-CSRF: 1` header on POST/PUT). The service asks
Frappe who the user is and which roles they have. Identity is never read from the request body.
If Frappe is unreachable, every call is refused (fail closed).

| Who | Endpoint | Purpose |
|---|---|---|
| public | `GET /health` | liveness |
| anyone | `GET /me` | user, roles, capabilities |
| agent | `POST /actions` | propose (only as the agent mapped to its account in `config/api.json`) |
| ingest | `POST /input-flags` | report a quarantined document |
| HR | `GET /actions?status=Proposed` | review queue |
| HR | `GET /actions/{id}` | details + audit trail (never the token) |
| HR | `POST /actions/{id}/approve` `{seen_hash}` | returns the approval token |
| HR | `POST /actions/{id}/reject` `{reason}` | |
| HR | `PUT /actions/{id}/payload` `{payload}` | edit: voids approval, re-runs checks |
| HR | `POST /actions/{id}/execute` `{token}` | Execution Gateway |
| HR | `GET /alerts`, `POST /alerts/{id}/ack` | monitor |
| HR | `GET /audit`, `GET /audit/verify` | audit trail + chain check |
| HR | `POST /fairness/disparity` | four-fifths test on a batch |

**Hardening**
- Strict request bodies (unknown fields → 422), `sha256:<64 hex>` source hashes,
  safe action ids, 256 KB body limit (also for chunked bodies), JSON depth limit.
- Rate limit per user, CORS allowlist (no wildcard), security headers, no stack traces.
- An agent proposing as another agent (`agent_impersonation`), or any non-HR account
  calling HR endpoints (`forbidden_endpoint`), is refused, audited and alerted.
- Job criteria for scoring come from `config/job_criteria.json` (trusted), never from the agent.
  Unknown job opening = no criterion accepted.

## Demo & manual testing
```bash
./venv/Scripts/python scripts/demo.py --start          # full scenario, ~5 s, exit 0 = all good
./venv/Scripts/python scripts/demo.py --start --step   # live presentation: Enter between scenes
```
`--start` launches `scripts/fake_frappe.py` (fictional Frappe, no Docker) and the API with a
throw-away database. Scenes: normal flow, fair screening, **prompt injection blocked**,
misbehaving agent, edit voids approval, monitor + tamper-evident audit log.

Against real services, set `DEMO_COMMS_TOKEN`, `DEMO_RECRUIT_TOKEN`, `DEMO_HR_TOKEN`
(`KEY:SECRET`) and pass `--api`, `--applicant`, `--applicant2`, `--malicious`, `--job`.

To explore by hand: `python scripts/fake_frappe.py --port 8090` (prints demo tokens), start
the API with `FRAPPE_URL=http://127.0.0.1:8090`, `FRAPPE_GATEWAY_API_KEY=demo-gateway`,
`FRAPPE_GATEWAY_API_SECRET=secret`, then use curl. `GET /__state` shows sent emails.

## Setup & run
```bash
cd guardrails
python -m venv venv
./venv/Scripts/python -m pip install -r requirements-dev.txt -c constraints.txt   # Windows
cp example.env .env        # then fill in the values (see comments inside)
./venv/Scripts/python -m pytest -q
./venv/Scripts/python -m uvicorn guardrails.settings:create_app_from_env --factory --port 8100
```
Frappe setup needed: an API key for the gateway user (write), one Frappe user per agent with the
`AI Agent` role (read-only), an ingest user, and HR users with the `HR Manager` role. List the
agent and ingest accounts in `config/api.json`.
