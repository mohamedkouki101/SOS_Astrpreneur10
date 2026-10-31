# agents/orchestrator — Orchestrator

**Responsibility:** Route chat requests, signed webhook events and scheduler triggers to the right agent; enforce per-agent tool allowlists and step/token budgets; call the external LLM with PII-masked context.

| File | Role |
|---|---|
| `orchestrator.stub` | Routing + allowlist enforcement |
| `budgets.stub` | Step/token budgets |
| `llm_client.stub` | External LLM call (provider/model TODO) |

**May depend on:** `context/`, `tools/`, `agents/`, `validation/`.
**Must never be depended on by:** `ingest/`, `validation/` (assumed: keeps the validator independent).

**Rules:** The LLM is external and untrusted. Only PII-masked context leaves the service. Read-only REST access only.
