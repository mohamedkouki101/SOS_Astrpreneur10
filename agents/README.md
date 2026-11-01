# agents — HR Flow agent service

9 agents (Orchestrator + 8 specialists), each a **LangGraph** graph, all using **qwen3:14b** (Ollama) as the decision model. Agents only **read** HR Flow (one read-only API user per agent) and **propose** actions; a human approves every proposal.

Design and tool lists: [`docs/agents.md`](../docs/agents.md).

## Setup (WSL)

```sh
cd SOS_Astrpreneur10        # repo root
export UV_PROJECT_ENVIRONMENT=$HOME/.venvs/rhflow-agents
uv sync --python 3.12
uv pip install --python $UV_PROJECT_ENVIRONMENT/bin/python \
  "en_core_web_sm @ https://github.com/explosion/spacy-models/releases/download/en_core_web_sm-3.8.0/en_core_web_sm-3.8.0-py3-none-any.whl"

# one read-only API user per agent (writes keys to .env, gitignored). Re-run after a site reset.
ADMIN_PASSWORD=... $UV_PROJECT_ENVIRONMENT/bin/python agents/scripts/setup_agent_users.py
```

Prerequisites: HR Flow at `http://localhost:8001` with the demo dataset, and Ollama at `http://localhost:11434` with `qwen3:14b`.

## Run

```sh
alias rhf="$HOME/.venvs/rhflow-agents/bin/python -m agents.cli"
rhf ask "Rank the applicants for the Senior Python Developer opening and propose the top 5 to HR."
rhf ask "Make a digest of the HR inbox."
rhf ask "How many casual leave days do I have left?" --as HR-EMP-00007
rhf run leave "Review the open leave applications and propose a decision for the first one."
rhf proposals          # proposals waiting for human approval
rhf ingest file.pdf    # see what the security pipeline does with a file
rhf graph              # Mermaid diagram of the orchestrator graph
rhf audit-verify       # check the hash chain of the agent audit trail
```

### Agent service (HR Chat + automatic inbox replies)

```sh
$HOME/.venvs/rhflow-agents/bin/python -m agents.integration.server   # 127.0.0.1:8200
```

HR Core calls it with signed requests (`hr_ai/webhooks.py` -> `agents/integration/server.py`):
HMAC-SHA256 over timestamp + event id + body, a 300 s replay window, and one run per event id.
- `POST /chat`: an HR Chat message (page `/chat`). The text went through the input security layer first;
  a held-back message never reaches this service. Intake (qwen, strict JSON) picks the persona, code binds
  it to the employee / job applicant HR linked to the account (User fields "Chat: linked ..."), then the HR
  Assistant answers with that persona's tools only. Requests become an `hr_request` AI Action for HR.
- `POST /events`: `inbound_email.received` for every released inbox mail; the Comms agent drafts a reply
  (AI Action "Proposed"). Failed runs are retried 3 times.

Needs `WEBHOOK_HMAC_SECRET` in `.env`, and the same value in HR Core site config `hr_ai_agents_webhook_secret`
(plus `hr_ai_agents_url = http://host.docker.internal:8200`). Ollama must be running (`ollama serve`).

HR mode: when an HR Manager (or Administrator) uses `/chat`, the `operator` agent finds the people named in the
message and prepares changes (interview reschedule/cancel, applicant status, new leave, leave approve/reject,
employee department/designation/branch/manager). Each one appears as a card with Confirm / Cancel; Confirm is the
human approval, then the Execution Gateway applies it. Create its read-only user once:
`RHFLOW_URL=http://localhost:8001 ADMIN_PASSWORD=... python agents/scripts/setup_agent_users.py operator`.

Chat demo account: `bench --site frontend execute hr_ai.setup.setup_demo_users --kwargs "{'chat_password': '...'}"`
creates the login `Utilisateur` (website user, no Desk) linked to a fictional employee and a fictional applicant.

Expect about 5–20 s per Qwen step. Ranking one opening (about 16 CVs) takes several minutes, so run it before the demo scene.

## Layout

| Folder | Content |
|---|---|
| `agents/` | `base.py` (the shared LangGraph loop + guardrails), `specs.py` (the 8 specialists: role, tools, budget) |
| `orchestrator/` | `orchestrator.py` (supervisor graph), `llm_client.py` (qwen3:14b), `budgets.py`, `audit.py` (hash-chained trail) |
| `ingest/` | Stages 1–4: `sanitizer.py`, `pii.py` (Presidio), `injection.py`, `provenance.py`, `pipeline.py` |
| `context/` | `context_builder.py` (trusted prompts vs `<data>`), `run_context.py` |
| `validation/` | `schemas.py` (Pydantic), `validator.py` (deterministic Output Validator) |
| `tools/` | `platform_client.py` (GET-only), `recruitment_tools.py`, `comms_tools.py`, `hr_tools.py`, `orchestrator_tools.py`, `outbox.py` (`propose_action`) |
| `integration/` | Signed webhook receiver: TODO (build-plan task 4) |
| `scripts/` | `setup_agent_users.py` |

Runtime files go to `var/` (gitignored): `audit.jsonl`, `alerts.jsonl`, `outbox/proposals.jsonl`, `token_map.json`.

## Rules enforced in code (design rules)
- **No write credentials.** `PlatformClient` only issues GET requests, and each agent's API role is read-only on its own DocTypes. This is verified: PUT, POST and DELETE return 403.
- **Allowlists.** Each agent is shown only its own tools. Any other tool name is rejected and audited.
- **Budgets.** Tool calls, steps, tokens and proposals per run.
- **External content is data.** It always goes through stages 1–4. Quarantined items return no text. Tool results are wrapped in `<data trust="untrusted">`.
- **PII.** It is masked before any model call. Applicant IDs (which are email addresses in v16) are replaced by `[APPLICANT_n]`.
- **One candidate per context.** Each CV is evaluated in its own isolated Qwen call; scores are computed by code.
- **The Output Validator is code.** It checks schema, cited source hashes, that evidence quotes exist in the source, and that there are no injected instructions or unmasked contact data.

## Input security layer
`INPUT_LAYER=local` (default, used by the tests) runs the built-in stages 1-4 in `agents/ingest/`.
`INPUT_LAYER=service` reads CVs and inbox emails only as signed envelopes from the input security layer
(`agents/tools/input_layer.py`): signature, trust and chunk hashes are checked, quarantined documents are never
used, and names the layer missed are masked again from the trusted record. Needs `INGEST_URL`,
`INGEST_ENVELOPE_API_KEY` and `INGEST_SIGNING_KEY` in `.env`. See `docs/manual-test.md`.

## Tests

```sh
$HOME/.venvs/rhflow-agents/bin/python -m pytest -q
```

The tests run offline: a scripted fake LLM stands in for Qwen and nothing calls HR Flow. There is one test file per guardrail (`tests/guardrails/`). The duplicate-webhook test is skipped until the receiver exists.

## Not done yet
- Malware scanner and injection classifier (hooks exist; not chosen yet).
- Where the PII token map lives long-term, and who may de-tokenize (open question).
