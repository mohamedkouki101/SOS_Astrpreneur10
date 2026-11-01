# 09 · Guardrails down: fail closed

**Story.** The guardrails service crashes in the middle of the day. A safe system must not "fail open" and
let agents through unchecked. HR Flow asks the guardrails at two moments (when a proposal arrives and right
before executing it); with no answer, it refuses in both places, even for an action HR has just approved.

Script: `scripts/s09_fail_closed.py` · about 30 seconds, no model. It stops the `guardrails-guardrails-1`
container and **always** restarts it (also on error).

## Layers exercised
| Step | Layer |
|---|---|
| `propose_action` → `/agent-actions/check` unreachable → refused | hr_ai ↔ guardrails bridge (`guardrails_client.py`) |
| Approve a pending action → gateway re-check unreachable → Failed | Execution Gateway |
| Interview record unchanged | HR Core |

## Show it live
1. HR chat: `Reschedule interview HR-INT-2026-0008 to 2026-11-25, same times.` (do not confirm yet)
2. Terminal: `docker stop guardrails-guardrails-1`
3. Click **Confirm**: the card ends **Failed: Guardrails gave no verdict (no answer): action refused (fail closed)**.
4. `docker start guardrails-guardrails-1`

## Expected
- New proposal while down: 403 "Guardrails gave no verdict … (fail closed)"; nothing in the HR queue.
- Approved while down: status `Failed`, same message; interview date unchanged.
- After restart: `/health` ok, everything works again.

## Last run (2026-11-01): 7/7 PASS
```
proposal accepted (AIA-2026-00040) · guardrails unreachable
proposal refused (Guardrails gave no verdict (no answer): action refused (fail closed))
action Failed (Guardrails gave no verdict (no answer): action refused (fail closed)) · interview unchanged (2026-11-03)
guardrails healthy again · /health ok
```

## Notes
- The same applies if the guardrails answer anything but a clear "allowed" (401, 403, 422, 503, bad JSON).
- A failed action is never retried silently; HR sees the reason on the card and can ask the agent again.
