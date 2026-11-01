# agents/agents — agent implementations

**Responsibility:** Domain agents that produce JSON proposals.

| File | Agent | Demo scope |
|---|---|---|
| `recruitment.stub` | Resume screening/ranking with job-related justifications | In scope (priority 2) |
| `comms.stub` | Email digest, draft replies | In scope (priority 4) |
| `assistant.stub` | Employee Q&A (RAG, vector store TODO) | Nice to have |
| `stubs.stub` | Onboarding, Leave, Payroll, Performance, Conflict | Stubbed |

One implementation with per-agent tool allowlists is acceptable (design rules).

**May depend on:** `tools/` (through the orchestrator's allowlist), `context/`.
**Must never be depended on by:** `validation/`, `ingest/`.

**Rules:** Proposals only, no writes. Scoring uses job-related criteria only. One candidate per context.
