# 05 · HR gives orders in the chat (HR mode)

**Story.** An HR Manager types plain orders in HR Chat. The **operator** agent (a read-only account) finds
the record, prepares the change and shows a change card. **Confirm** is the human approval: a signed token
bound to the action hash; the Execution Gateway then applies it. **Cancel** changes nothing.

Script: `scripts/s05_hr_chat_operator.py` · about 3 minutes (three model answers)

## Layers exercised
| Step | Layer |
|---|---|
| Administrator / HR Manager → HR mode (120 messages / hour) | Interface + roles |
| Message → input layer first; names typed by HR matched to record ids by code (`people`) | Input layer + context builder |
| Operator agent: `interview_update`, `employee_update` (read-only REST key) | Agent layer, tool allowlist |
| Guardrails rule `hr_operator`: record-bound kinds, subject = the record in the payload | Guardrails: policy |
| Confirm = `AI Action.approve()` → token = HMAC(name, hash, approver) | Human approval |
| Gateway: hash + token + guardrails re-check → `Interview.scheduled_on` updated | Execution Gateway |
| Cancel = `reject()` → Rejected, nothing written | Human approval |

## Show it live
Browser tab http://localhost:8001/chat as Administrator, press **Clear**:
1. `Reschedule interview HR-INT-2026-0008 to 2026-11-18, same times.`
   → card "Reschedule interview HR-INT-2026-0008 to 2026-11-18 15:00:00 - 15:45:00" → **Confirm** → **Executed**.
2. `Move employee Rim Sfaxi (HR-EMP-00006) to the Sousse branch.` → card → **Cancel** → Rejected.
3. Put the interview back: `Reschedule interview HR-INT-2026-0008 to 2026-11-11, same times.` → **Confirm**.

## Expected
- Each order gives exactly one change card with a Confirm button (`can_confirm`).
- Confirm → status Approved → Executed in a few seconds; the Interview record shows the new date.
- Cancel → Rejected; the employee's branch is unchanged.

## Last run (2026-11-01): 11/11 PASS
```
HR> Reschedule interview HR-INT-2026-0008 to 2026-11-18, same times.
change card: Reschedule interview HR-INT-2026-0008 to 2026-11-18  15:00:00 - 15:45:00     (AIA-2026-00043)
Executed by the Execution Gateway (Interview HR-INT-2026-0008 moved to 2026-11-18 15:00:00-15:45:00)
HR> Move employee Rim Sfaxi (HR-EMP-00006) to the Sousse branch.
change card: Update employee HR-EMP-00006: branch -> Sousse                                (AIA-2026-00044)
cancel accepted ({'status': 'Rejected'}) · branch still Tunis HQ
HR> Reschedule interview HR-INT-2026-0008 to 2026-11-11, same times.  -> interview back on 2026-11-11
```

## Notes
- Only job fields (department, designation, branch, reports_to) can ever be changed by `employee_update`;
  personal data is out of reach of the gateway handler.
- A forged version of this (subject employee 00007, payload employee 00001) is refused: see S07.
