# Contracts (TODO)

Tables only. No code. All entries TODO unless stated in CLAUDE.md.

## REST endpoints
| Method | Path | Caller | Auth | Purpose | Status |
|---|---|---|---|---|---|
| GET | `/api/resource/<DocType>` | agent service user | `token KEY:SECRET` (read-only role) | Read records | Given by HR Core |
| POST | `/api/method/hr_ai.api.propose_action` | agent service user | `token KEY:SECRET` | Create AI Action (Proposed) | TODO: payload schema |
| ? | TODO | Chat box | TODO | Chat request to Orchestrator | TODO |

Note: CLAUDE.md §6 mentions `POST /api/resource/<DocType>`, but agents must not write (§7). Agent roles must therefore not allow POST on resources (assumed).

## Webhook payloads (HR Core → agents)
| Event | Trigger | Fields | Signature header | Idempotency key | Status |
|---|---|---|---|---|---|
| TODO | doc_events (e.g. new Job Applicant) (assumed) | TODO | TODO (HMAC) | doc name + event id | TODO |
| TODO | new email record (if HR Core Email Account used) | TODO | TODO | doc name + event id | TODO: verify support |
| TODO | scheduler trigger | TODO | TODO | TODO | TODO |

Replay protection mechanism (timestamp window / nonce): TODO.

## AI Action fields
| Field | Type | Description | Status |
|---|---|---|---|
| kind | TODO | Action type | From CLAUDE.md |
| subject link | TODO | Linked document | From CLAUDE.md |
| payload | JSON | Proposed action | From CLAUDE.md |
| action_hash | TODO | SHA-256 of canonical payload | From CLAUDE.md; canonicalization TODO |
| source_hashes | TODO | Cited source SHA-256s | From CLAUDE.md |
| agent | TODO | Proposing agent | From CLAUDE.md |
| status | Select | Proposed / Approved / Rejected / Executed / Failed | From CLAUDE.md |
| approver | Link User | Approving human | From CLAUDE.md |
| approval_token | TODO | Signed token bound to action_hash | From CLAUDE.md; key mgmt TODO |

## Output Validator schemas
| kind | Schema file | Status |
|---|---|---|
| TODO | `agents/validation/schemas/` | TODO |
