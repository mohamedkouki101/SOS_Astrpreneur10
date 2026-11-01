# 08 · Signed webhooks: forgery, replay and duplicates

**Story.** HR Core tells the agent service about new events (a released email, a chat message) over HTTP.
Anyone on the network could try to call that endpoint to make the agents act. Every call must carry an
HMAC signature over timestamp, event id and body; old requests are refused; and one event id runs at most once,
however often it is delivered.

Script: `scripts/s08_signed_webhooks.py` · about 5 seconds, no model

## Layers exercised
| Step | Layer |
|---|---|
| `X-RHFlow-Signature = sha256=HMAC(secret, "{ts}.{event_id}." + body)` | Integration: signed webhook (`agents/integration/signing.py`) |
| `X-RHFlow-Timestamp` outside the 300 s window → refused | Replay protection |
| Event id stored in `var/events.sqlite`; repeat → `duplicate` | Idempotency |
| Every rejection → `webhook_rejected` alert | Monitor (agent side) |

## Attempts
| Attempt | Expected |
|---|---|
| No signature headers | 401 |
| Signed with the wrong key | 401 |
| Body changed after signing (`IN-2026-99999` → `IN-2026-00001`) | 401 |
| Header event id ≠ body event id | 401 |
| Correctly signed, but 1 hour old | 401 |
| Re-delivery of an event that already ran (correctly signed), twice | 200 `duplicate`, no new run |

## Show it live
`$P "Test scenarios/scripts/s08_signed_webhooks.py"`, then `tail -5 var/alerts.jsonl`.

## Last run (2026-11-01): 8/8 PASS
```
unsigned request rejected (401) · wrong key rejected · body changed after signing rejected
header / body event id mismatch rejected · 1-hour-old signed request rejected
re-delivery of inbound_email:IN-2026-00016:79daf4326f8a4c69 answered 'duplicate' ({"status":"duplicate","previous":"done"})
second re-delivery: still duplicate — the original run produced []; no new agent run
alerts: missing or malformed signature headers · bad signature ×3 · timestamp outside the replay window
```

## Notes
- The automated counterpart is `tests/guardrails/test_duplicate_webhook.py` (run in S12).
- The body sent by HR Core never contains document text, only record names and report ids: the agents fetch
  the masked, signed envelope from the input layer themselves.
