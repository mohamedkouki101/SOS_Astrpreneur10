# Security

Golden rule: **the agent executes, the human decides about people.** Nothing is sent or recorded without human approval.

## Principles → enforcement point
| Rule (CLAUDE.md §7) | Enforced in |
|---|---|
| Never edit upstream platform code | Repo layout; upstream code is gitignored, untouched |
| Agents hold no write credentials | `agents/tools/platform_client` (read-only token) |
| Only Execution Gateway writes, approved only, approval ↔ hash verified, policy re-checked, idempotent | `hr_ai/hr_ai/executor.py`, `approval.py` |
| External content is DATA, never in system prompt; stages 1–4 always | `agents/ingest/`, `agents/context/` |
| Mask PII before scoring/LLM | `agents/ingest/pii` |
| Job-related scoring only; disparity test | `hr_ai/hr_ai/fairness.py` |
| One candidate per context; allowlists; budgets | `agents/context/`, `agents/orchestrator/` |
| Validator is code; rejects off-schema / no source hash / unknown tool | `agents/validation/` |
| Policy default-deny; never loosened to pass tests | `hr_ai/hr_ai/policy.py` |
| Agents never touch MariaDB | `agents/` network/credential setup |
| Webhooks HMAC + replay + idempotent | `hr_ai/hr_ai/webhooks.py`, `agents/integration/` |
| No secrets in prompts/logs/git | `.env` (gitignored), `.env.example` names only |
| Audit every proposal/approval/execution; append-only, hash-chained | `hr_ai/hr_ai/audit.py` |
| Fictional data only | `data/` |
| Permission-aware reads (permission-aware list API, never the unchecked one) | `hr_ai` code reachable by agents |

## Trust boundaries
- **Authentication:** API Gateway (SSO/OAuth2 + MFA, RBAC, rate limit, CSRF/CORS, TLS, chat signature).
- **Enforcement:** between Output Validator and Policy Engine; nothing from the AI layer crosses without passing guardrails.
- **External LLM:** untrusted; receives masked context only.

## Required guardrail tests
| Test | Placeholder |
|---|---|
| Injection CV blocked (demo) | `tests/guardrails/test_injection.stub` |
| Approval bypass / edited payload refused | `tests/guardrails/test_bypass.stub` |
| Score disparity | `tests/guardrails/test_disparity.stub` |
| Duplicate / replayed webhook | `tests/guardrails/test_duplicate_webhook.stub` |

## Undecided (TODO)
Malware scanner, injection classifier, Secrets Manager, approval-signing key management, de-tokenization point and permissions, token map storage, Audit Log storage, Monitor alert channel.

Also TODO: change default Administrator/admin login.
