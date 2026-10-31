# Architecture

Source: CLAUDE.md §4–§6.

## Layers (top to bottom)
1. **Users:** HR Team, Manager, Employee.
2. **Interfaces:** Desk (admin UI), Chat (demo: simple chat box), Mobile PWA.
3. **Trust boundary: authentication.** API Gateway: SSO/OAuth2 + MFA, RBAC, rate limiting, CSRF/CORS, TLS, chat signature check.
4. **Secure Integration Layer:** Secrets Manager (TODO), Webhooks/doc_events (HMAC, replay-protected), Scheduler (IMAP pull, reminders, digest), REST API (scoped read-only tokens for agents).
5. **Agentic AI Layer:** Context Builder → Orchestrator → Agents → Output Validator. External LLM (untrusted).
6. **Trust boundary: enforcement.**
7. **Guardrails & Execution Control:** Policy Engine → Fairness checks → Human approval → Execution Gateway; Audit Log; Monitor.
8. **HR Core:** framework, HR modules, MariaDB (encrypted PII fields).

**Untrusted Input Zone:** resumes and emails enter only through Sanitizer → PII mask → Injection scan → Provenance tag.

## Where each component runs
| Component | Location | Process |
|---|---|---|
| API Gateway | HR Core/edge (details TODO) | — |
| Webhooks, Scheduler | `hr_ai/hr_ai/hooks.py`, `webhooks.py` | HR Core |
| AI Action queue | `hr_ai/hr_ai/ai_action/`, `api.py` | HR Core |
| Policy, Fairness, Approval, Executor, Audit, Monitor | `hr_ai/hr_ai/*.py` | HR Core |
| Input pipeline | `agents/ingest/` | Agent service |
| Context Builder | `agents/context/` | Agent service |
| Orchestrator + LLM client | `agents/orchestrator/` | Agent service |
| Agents | `agents/agents/` | Agent service |
| Output Validator | `agents/validation/` | Agent service |
| Webhook receiver | `agents/integration/` (assumed) | Agent service |

Placing Fairness checks in `hr_ai` follows CLAUDE.md §9 (`hr_ai/` = "AI Action, policy, fairness, approval, executor, audit").

## Data flows
1. **Inbound content:** CV/email → `ingest` stages 1–4 → Context Builder → Orchestrator. Raw originals stored in HR Core. Injection hit → quarantine + alert.
2. **User requests:** User → Interface → API Gateway → (chat) Orchestrator, or → scoped REST API → HR Core.
3. **Triggers:** HR Core doc_events → signed webhook → `integration/` receiver → Orchestrator. Scheduler → Orchestrator (IMAP pull, reminders, digest).
4. **Agent reads:** Orchestrator/tools → HR Core REST with per-agent read-only token. Never MariaDB.
5. **LLM calls:** Orchestrator → External LLM with PII-masked context only. Response treated as untrusted.
6. **Proposal path:** Agent → Output Validator → `propose_action` (AI Action = Proposed) → Policy Engine → Fairness → Human approval (token bound to action_hash) → Execution Gateway (verify, re-check policy, idempotent) → HR Core. Policy, Approval, Execution each write to Audit Log → Monitor.

Exact placement of Policy/Fairness before vs. after record creation: (assumed) as above; diagram shows Validator → Policy → Fairness → Approval → Executor.
