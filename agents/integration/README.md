# agents/integration — agent HTTP service (port 8200)

`python -m agents.integration.server` (repo root). Binds to `127.0.0.1:8200` (`AGENTS_HOST` / `AGENTS_PORT`);
the platform containers reach it through `host.docker.internal`.

| Endpoint | Caller | What it does |
|---|---|---|
| `GET /health` | anyone | liveness |
| `POST /chat` | `hr_ai.chat` (signed) | HR Chat: intake picks the persona, code binds it to the identities HR linked on the user, then the HR Assistant (employees, candidates) or the Operator (HR) answers |
| `POST /events` | `hr_ai.webhooks` (signed) | new inbox mail → Comms agent drafts a reply; returns 202 at once, runs in a worker |

Every request is verified in `signing.py`: HMAC-SHA256 over timestamp, event id and body (`WEBHOOK_HMAC_SECRET`),
replay window (`WEBHOOK_REPLAY_WINDOW_SECONDS`, default 300 s), and idempotency on the event id (`events.py`):
a duplicate delivery never starts a second run.
