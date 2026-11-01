# 10 · Orchestrator, the Leave agent, and stubbed agents

**Story.** HR asks in plain language: *"Review leave application HR-LAP-2026-000NN and propose a decision for
HR."* The Orchestrator routes the request to the Leave agent, which reads the application, checks team
absences and the leave policy, and proposes a decision with a factual reason. HR allows it and the gateway
submits the Leave Application. A stubbed agent (Payroll) is out of demo scope: it has no allow rule in the
guardrails, so whatever it proposes is denied.

Script: `scripts/s10_orchestrator_agents.py` · about 2 minutes. It picks the newest **open** leave
application (never HR-LAP-2026-00009, which S04 uses), so every run decides a different fictional one.

## Layers exercised
| Step | Layer |
|---|---|
| `agents.cli ask` → Orchestrator (LangGraph) → `delegate(agent="leave")` | Orchestrator, routing |
| Leave agent tools only: `get_leave_application`, `get_team_absences`, `search_policies`, `propose_leave_decision` | Per-agent tool allowlist + budget |
| Proposal must cite the application's source hash (`read it first`) | Output validator |
| `decide_leave_application` allowed for `leave_agent` | Guardrails: policy |
| Allow → gateway `Leave Application.submit()` with the proposed status | Human approval + Execution Gateway |
| Payroll `payroll_reply` → `tool_not_allowed` | Guardrails default deny for stubs |
| `agents.cli audit-verify` | Agent-side audit chain |

## Show it live
```bash
$P -m agents.cli ask "Review leave application HR-LAP-2026-00011 and propose a decision for HR."
```
The tool calls are printed as they happen. In RH AI, the **Leave decision** card shows "Approved -
HR-LAP-…" with the reason. **Allow** → Executed → the Leave Application is submitted.

## Expected
- Delegation to `leave` only; no other agent's tools used.
- One `leave_decision` proposal (agent `leave`) with a reason based on balance, team absences and policy.
- After Allow: Executed, the Leave Application is submitted (`docstatus 1`) with the proposed status.
- Payroll: nothing reaches the HR queue; a direct proposal with the payroll key → `tool_not_allowed`.
- Agent audit chain OK.

## Last run (2026-11-01): 9/9 PASS
```
HR-LAP-2026-00006: Skander Jaziri, Casual Leave 2026-11-16 -> 2026-11-17, status Open
→ delegate to [leave]
    · leave.get_leave_application · leave.get_team_absences(Human Resources - B, 2026-11-16..17)
    · leave.search_policies("Casual Leave") · leave.propose_leave_decision(Approved, ...)
proposal AIA-2026-00046 [leave_decision] (75 s)
reason: "2 days of Casual Leave for moving house. Remaining balance 10 days, no team absences in the same
        department during the requested dates, submitted in advance, no conflicts with the leave policy."
Executed (Leave application HR-LAP-2026-00006 approved) · submitted as Approved
payroll.get_salary_slip -> "No salary slip was found" (nothing proposed)
payroll_reply from the payroll agent: Blocked by guardrails: tool_not_allowed
agent audit chain verified (audit chain OK (798 entries))
```

## Notes
- The demo data has no salary slips, so the live Payroll run finds nothing to propose; the script then sends
  the proposal a payroll agent would send (same HTTP call, payroll agent's own key) to show the default deny.
- Onboarding, Performance and Conflict are stubbed the same way (empty tool list in `guardrails/config/policy.json`).
