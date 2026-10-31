# agents/integration — webhook receiver (folder assumed)

**Responsibility:** Receive HR Core → agents events. Verify HMAC, reject replays, dedupe on idempotency key (doc name + event id), enqueue, return fast.

**May depend on:** `orchestrator/` (enqueue target).
**Must never be depended on by:** `hr_ai` (it only POSTs to it).

**Rules:** Verify HMAC and replay protection; handlers idempotent. Secrets from `.env`.
