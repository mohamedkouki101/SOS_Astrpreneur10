# hr_ai — HR Core custom app

**Responsibility:** In-process extension of HR Core. Holds the AI Action queue and every guardrail that decides or performs writes.

**Implements:** AI Action DocType + workflow, Policy Engine, Fairness checks, Human approval, Execution Gateway, Audit Log, Monitor, Webhooks/doc_events, Scheduler hooks.

| File | Component |
|---|---|
| `hr_ai/hooks.py` | doc_events, scheduler_events |
| `hr_ai/api.py` | whitelisted `propose_action` |
| `hr_ai/webhooks.py` | signed outbound events |
| `hr_ai/policy.py` | Policy Engine |
| `hr_ai/fairness.py` | Fairness checks |
| `hr_ai/approval.py` | Human approval token |
| `hr_ai/executor.py` | Execution Gateway |
| `hr_ai/audit.py` | Audit Log |
| `hr_ai/monitor.py` | Monitor |
| `hr_ai/ai_action/` | AI Action DocType (JSON not yet created) |

The nested `hr_ai/hr_ai/` package follows the platform's standard app layout (assumed).

**May depend on:** HR Core framework and HR module APIs (Python calls, hooks).
**Must never be depended on by:** `agents/` code importing it. Agents reach it only over REST (`/api/method/hr_ai.api.*`, `/api/resource/*`).

**Rules (CLAUDE.md §7):**
- Never edit upstream platform code; extend only via this app, hooks and DocTypes.
- Only the Execution Gateway writes, only for Approved actions, after verifying approval ↔ action_hash and re-checking policy. Idempotent.
- Policy Engine is default-deny; never loosen defaults to pass a test.
- Every proposal, approval and execution writes an Audit Log entry; entries are never updated or deleted.
- Use the permission-aware list API, never the permission-ignoring one, in anything agents can reach.
- Webhooks: HMAC + replay protection; handlers idempotent.
- Deployment: must be built into the image (`custom:N`); never installed only in a running container.
