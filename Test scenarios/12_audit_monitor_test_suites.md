# 12 · Audit trail, monitor and the automated test suites

**Story.** For the jury and for an auditor: (1) the automated tests, one per guardrail; (2) the two audit
trails are hash-chained, and rewriting one line of history is detected; (3) the Monitor's alerts.

Script: `scripts/s12_audit_monitor_tests.py` · about 1 minute, no model

## Part 1: automated test suites
| Suite | Command | Result (2026-11-01) |
|---|---|---|
| Agents + hr_ai guardrail tests: injection, bypass, disparity, duplicate webhook, input layer, PII, chat, orchestrator | `$P -m pytest -q tests` (repo root) | **67 passed** |
| Guardrails service: policy, fairness, approval, gateway, audit storage, monitor, auth, RH Chat rules | `cd guardrails && $P -m pytest -q` | **362 passed** |
| Input security layer: file types, extraction, hidden text, unicode, PII, scanner, signing, API contract | `cd input-security-layer && .venv/bin/python -m pytest -q` | **367 passed** |

796 tests in total, all green.

## Part 2: audit trails
| Trail | What it records | Check |
|---|---|---|
| Agent side `var/audit.jsonl` | every agent run, tool call, proposal, failed submission, webhook, alert | `$P -m agents.cli audit-verify` → `audit chain OK (N entries)` |
| Guardrails `GET /audit/verify` (HR only) | API denials, monitor alerts, disparity checks | `{"ok": true, "entries": N, "head": "sha256:…"}` |
| HR Core | AI Action version history (approve / edit / decline), gateway comments | open the AI Action → timeline |

Tamper test: the script copies `audit.jsonl` to a temp folder, rewrites the `agent` field of one entry,
and runs `audit-verify` on the copy (`AGENTS_VAR_DIR`): **BROKEN**. The live file is never touched.

## Part 3: monitor
- Guardrails `GET /alerts`: `instant` / `repeated_denials` / `adverse_impact` alerts (S03 adds one).
- Agent side `var/alerts.jsonl`: `injection_quarantine`, `webhook_rejected`, `chat_reply_blocked`.
- An employee gets 403 on the audit endpoints.

## Last run (2026-11-01): 10/10 PASS
```
agents + hr_ai guardrail tests (67 passed) · guardrails service (362 passed) · input security layer (367 passed)
live chain verified (audit chain OK (808 entries)) · edited entry #2 in a copy -> chain BROKEN
guardrails chain verified (24 entries) · events: monitor.alert=12, api.denied=10, monitor.disparity_checked=2
[high] repeated_denials: guardrails.check@rhflow.local was denied 3 times in 600s
agent-side alerts: injection_quarantine=8, webhook_rejected=6, chat_reply_blocked=3
an employee cannot read the audit log (403)
```

## Notes
- The `forbidden_endpoint` / `repeated_denials` alerts for `guardrails.check@` date from before the guardrails
  wiring was finished (early 2026-11-01). Acknowledge them before the demo.
- Gap (README finding 4): `hr_ai/audit.py` and `hr_ai/monitor.py` are still placeholders, so refused
  proposals sent straight to `propose_action` are not stored in any audit log.
