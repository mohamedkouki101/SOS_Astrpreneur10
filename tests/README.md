# tests — cross-layer tests

One test (or more) per guardrail, run offline: a scripted fake model stands in for qwen, and the fictional
dataset in `platform/Data_set/dataset` stands in for real CVs and emails.

| Path | Content |
|---|---|
| `guardrails/test_injection.py` | Poisoned CVs and emails are caught by stages 1–4 and never reach the model |
| `guardrails/test_bypass.py` | A tricked agent cannot write, call unknown tools or exceed its budgets |
| `guardrails/test_disparity.py` | Rankings are job-related only; protected attributes are blind |
| `guardrails/test_duplicate_webhook.py` | Signed webhooks: bad signature, replay and duplicate events are rejected |
| `guardrails/test_input_layer.py`, `test_pii.py` | Envelope signature checks, PII masking |
| `agents/` | Orchestrator routing, Comms placeholders, HR Chat |

```bash
uv run pytest -q tests      # repo root: 67 passed
```

The `hr_ai` ↔ guardrails bridge is tested in `guardrails/tests/test_hr_ai_bridge.py`.
**Rules:** external content goes through stages 1–4 even in tests. Never loosen Policy Engine defaults to pass a test. Fictional data only.
