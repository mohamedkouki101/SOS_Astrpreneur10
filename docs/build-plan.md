# Build plan (skeleton phase)

From CLAUDE.md §10. Propose a plan and get approval before each step; small steps, commit after each, run the check, report results.

| # | Task | Check | Status |
|---|---|---|---|
| 0 | Repo skeleton + docs | Tree matches plan; no implementation code | Done |
| 1 | `hr_ai` app skeleton + AI Action DocType + workflow | Record created and approved in Desk | TODO |
| 2 | `propose_action` whitelisted method + API user | `curl` creates a Proposed record; permissions enforced | TODO |
| 3 | Executor hook on Approved | Unapproved or edited record refused; duplicate runs once | TODO |
| 4 | `agents/` service: signed webhook receiver + stub tools | Bad signature rejected; replay rejected | TODO |
| 5 | Fictional data in `data/` | TODO (no check given in CLAUDE.md) | TODO |

Then: plan intelligence and security pipeline.

Prerequisites / notes:
- Task 1 requires adding `hr_ai` to `apps.json` and rebuilding the image (`custom:N`, 10–15 min); never install only in a running container.
- Task 4 requires choosing the agent service language/framework (open question).
- TODO verify: scheduler disabled on fresh site (enable it on the site).
