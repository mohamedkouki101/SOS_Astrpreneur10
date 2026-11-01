# hr_ai — our Frappe app inside HR Flow

Runs in-process in Frappe HR (built into the platform image, see `platform/build-images.sh`). It holds the
human side of every AI action and is the **only writer**: agents never get write credentials.

| File | What it does |
|---|---|
| `hooks.py` | `doc_events` (AI Action → executor, Job Applicant → input layer), scheduler (inbox sync every minute), RH AI tile |
| `api.py` | `propose_action` (agents' only way in: creates a *Proposed* AI Action after guardrails check #1), RH AI queue API |
| `approval.py` | Canonical action hash + approval token (HMAC over action, hash, approver). Any edit voids the approval |
| `executor.py` | **Execution Gateway**: re-checks hash, token and guardrails (check #2), resolves `<PERSON_n>` tokens, writes or sends; idempotent, fails closed |
| `guardrails_client.py` | Bridge to `guardrails/` (policy, fairness) |
| `ingest.py` | Bridge to `input-security-layer/`: CVs and Mailpit inbox → stages 1–4, HR **Security checks** (inspect / release / reject) |
| `webhooks.py` | Signed events to the agent service (HMAC, timestamp, idempotency key) |
| `chat.py`, `www/chat.*` | **RH Chat** (`/chat`): employees, candidates and HR; every message goes through the input layer first |
| `desk.py` | Apps screen and website home page for chat users |
| `setup.py` | Install/migrate hooks; `setup_guardrails_accounts`, `setup_demo_users` (called by `scripts/setup_stack.py site`) |
| `hr_ai/doctype/` | **AI Action** (Proposed → Approved / Rejected → Executed / Failed), **Inbound Email**, **Chat Message** |
| `hr_ai/page/rh_ai/` | **RH AI** decision page (`/desk/rh-ai`) |

Site config keys (all set by `scripts/setup_stack.py site`, never in git): `hr_ai_ingest_*`, `hr_ai_guardrails_*`,
`hr_ai_agents_url`, `hr_ai_agents_webhook_secret`, `hr_ai_mailpit_url`, `hr_ai_hr_inbox`, `hr_ai_test_recipient`,
`hr_ai_approval_secret` (generated on install).

**Rules:** never patch `hrms`, `erpnext` or `frappe`; extend through this app. Only Approved actions are executed,
after the approval ↔ hash check and a fresh guardrails check. Permission-aware queries (`frappe.get_list`) in
anything agents can reach. Test mode: every outgoing email goes to `hr_ai_test_recipient`.
