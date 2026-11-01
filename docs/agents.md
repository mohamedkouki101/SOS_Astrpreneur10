# Agents: roles, tools and decision model

**Framework:** LangGraph (each agent is a compiled `StateGraph`; the Orchestrator is a supervisor graph with one node per specialist). **Code:** `agents/` (see `agents/README.md`).

**Decision model:** `qwen3:14b`, served by Ollama (`http://localhost:11434`). It is the decision model for all 9 agents: at every step it picks which of its allowed tools to call, then writes the answer or proposal.
**Verified:** with the Ollama `tools` API, qwen3:14b picked the correct tool and arguments (`get_leave_balance`, ~10 s including model load, `think: false`).

## One loop for all agents

All agents share one LangGraph implementation (`agents/agents/base.py`): `model` node (qwen3:14b with bound tools) → `guarded_tools` node (our own, not the prebuilt ToolNode) → back to `model`. Each agent has its own **system prompt, tool allowlist and budgets** (design rules allows this).

```
task ─► Qwen (system prompt + task + only THIS agent's tools)
          │ tool_call?
          ├─ yes ─► code checks: tool in allowlist? args valid (Pydantic)? budget left?
          │           └─ run tool (read-only) ─► result wrapped as <tool_result>DATA</tool_result> ─► back to Qwen
          └─ no / propose_* called ─► Output Validator (code) ─► AI Action "Proposed" ─► human approval
```

Qwen decides; code enforces the limits:

| Guardrail | Enforced by code |
|---|---|
| Tool allowlist | Only the agent's tools are sent to Qwen. Any other tool name it returns is rejected and logged. |
| Arguments | Validated against each tool's schema; enums wherever possible. |
| Budgets | Max tool calls per run (default 6), max tokens, timeout. When exceeded, the run stops with no proposal. |
| No writes | No agent has a write tool. The only output is `propose_*`, which creates a **Proposed** AI Action. The Execution Gateway in `hr_ai` writes after human approval. |
| Untrusted content | `get_applicant_cv`, `read_email`, `get_grievance` always run ingest stages 1–4. A quarantined item returns `{"status": "QUARANTINED"}` and no text. |
| One candidate per context | The Recruitment agent handles one applicant per run; the Orchestrator fans out. |
| Scoring | Qwen gathers evidence; the score comes from `score_candidate` (deterministic code, job-related criteria only). |
| Read access | All reads go through the agent's own read-only HR Flow API user. No database access. |

## The 9 agents and their tools

The names come from the architecture diagram (assumed: confirm). ★ = in demo scope; the others are stubs that work but are kept minimal.

### 0. Orchestrator ★
Routes requests, events and scheduled triggers to one specialist agent. It cannot call domain tools itself.

| Tool | Purpose |
|---|---|
| `delegate(agent, task)` | `agent` ∈ the 8 specialists (enum). Runs that agent's loop and returns its result. |
| `list_pending_events()` | New applicants, new emails, open leave requests, scheduled digests. |
| `get_ai_action_status(action_id)` | Whether a previous proposal is Proposed, Approved, Rejected or Executed. |

Budget: max 3 delegations per request.

### 1. Recruitment ★
| Tool | HR Flow source | Notes |
|---|---|---|
| `get_job_opening(opening)` | Job Opening | Requirements, skills, minimum years |
| `list_applicants(opening, status?)` | Job Applicant (`job_title` = opening link) | IDs and status only, no PII |
| `evaluate_applicant(applicant)` | Job Applicant → `resume_attachment` | Stages 1–4, then Qwen extracts evidence in an isolated context; code scores |
| `evaluate_all_applicants(opening)` | same, one CV per isolated context | Returns a ranking table + quarantined list |
| `get_free_slots(date_from, date_to)` | calendar JSON | For interview proposals |
| `list_interviews(applicant)` | Interview | Avoids double booking |
| `propose_ranking(opening, ranking[])` | → AI Action | Score, justification and evidence quotes with source hashes |
| `propose_applicant_status(applicant, status, reason)` | → AI Action | Shortlisted / Rejected / Hold |
| `propose_interview(applicant, slot, interview_type)` | → AI Action | |

### 2. Comms (inbox) ★
| Tool | Source | Notes |
|---|---|---|
| `list_new_emails(since?)` | `.eml` folder (stand-in for the mailbox) | IDs, senders (masked) and subjects |
| `read_email(email_id)` | `.eml` | Through stages 1–4 |
| `find_applicant_by_email(email_token)` | Job Applicant | Links an email to an application |
| `get_applicant_status(applicant)` | Job Applicant | For accurate replies |
| `search_policies(query)` | policy files | Facts for replies |
| `propose_email_reply(email_id, subject, body)` | → AI Action | Recipient stays a token until execution |
| `propose_digest(items[])` | → AI Action | Priority, category, one-line summary |

### 3. HR Assistant (employee Q&A) ★ nice-to-have
| Tool | Source | Notes |
|---|---|---|
| `search_policies(query)` | policy files | Simple RAG |
| `get_my_profile()` | Employee | **Caller's own record only** (bound to the session, not an argument) |
| `get_my_leave_balance(leave_type)` | Leave Ledger Entry | Caller's own balance only |
| `get_holiday_list(year)` | Holiday List | |

Answers only; no proposals.

### 4. Leave
| Tool | Source |
|---|---|
| `list_leave_applications(status?)` | Leave Application |
| `get_leave_application(id)` | Leave Application |
| `get_leave_balance(employee, leave_type)` | Leave Allocation / Leave Ledger |
| `get_team_absences(department, date_from, date_to)` | Leave Application + Attendance |
| `get_holiday_list(year)` | Holiday List |
| `search_policies("leave")` | policy files |
| `propose_leave_decision(application, decision, reason)` | → AI Action (HR approves) |

### 5. Onboarding (stub)
| Tool | Source |
|---|---|
| `list_new_joiners(days)` | Employee (`date_of_joining`) |
| `get_employee(employee)` | Employee (no personal fields) |
| `get_onboarding_template(designation)` | Employee Onboarding Template |
| `propose_onboarding_tasks(employee, tasks[])` | → AI Action |
| `propose_welcome_email(employee, body)` | → AI Action |

### 6. Payroll (stub, read and explain only)
| Tool | Source |
|---|---|
| `get_salary_slip(employee, month)` | Salary Slip (no seed data yet) |
| `get_attendance_summary(employee, month)` | Attendance |
| `search_policies("expenses")` | policy files |
| `propose_payroll_reply(employee, body)` | → AI Action |

It never proposes salary changes.

### 7. Performance (stub)
| Tool | Source |
|---|---|
| `list_appraisals(cycle)` | Appraisal |
| `get_goals(employee)` | Goal |
| `get_feedback(employee)` | Employee Performance Feedback |
| `propose_review_summary(employee, summary)` | → AI Action |

It summarises only; it never rates people.

### 8. Conflict (stub)
| Tool | Source |
|---|---|
| `list_grievances(status?)` | Employee Grievance |
| `get_grievance(id)` | Employee Grievance (through stages 1–4) |
| `search_policies("code of conduct")` | policy files |
| `propose_grievance_triage(id, category, priority, next_step)` | → AI Action |

It never decides about people; it only triages for HR.

## Qwen settings
- `think: false` for tool steps (speed). `think: true` is allowed only for the final ranking justification, if time permits.
- `temperature: 0.1`, `num_ctx: 16384`.
- Tool descriptions are short, arguments use enums, and each agent has at most 9 tools. Fewer tools means better choices from an 8B model.

## Runtime
- The agent service runs in WSL (Python 3.12, venv) for fast iteration. It reaches HR Flow at `http://localhost:8001` and Ollama at `http://localhost:11434`.
- Ollama does not currently answer at `host.docker.internal`, so containerising the agents later requires exposing Ollama (`OLLAMA_HOST=0.0.0.0`).
- Expected latency: about 3–10 s per Qwen step, so 20–60 s per agent run. For the demo, run the ranking before the scene and show the result.

## Implementation notes
- Applicant record names in v16 are email addresses. Tools replace them with pseudonyms `[APPLICANT_n]`; only code resolves them.
- Read-only agent users are created by `agents/scripts/setup_agent_users.py` (verified: PUT/POST/DELETE → 403, other DocTypes → 403).

## Open
- Confirm the 9 agent names.
- Seed data for Payroll, Performance, Conflict and Onboarding does not exist yet (stubs).
