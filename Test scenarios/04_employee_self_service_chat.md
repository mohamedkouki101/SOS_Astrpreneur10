# 04 · Employee self-service in HR Chat

**Story.** Khalil Ouali (employee HR-EMP-00007, logged in as website user *Utilisateur*) asks the HR
assistant about his leave, the leave policy, a colleague's salary, and a work certificate. He gets answers
from **his own** HR data only, the colleague's data is refused, and the certificate request becomes a card
in the HR queue. He cannot approve anything himself.

Script: `scripts/s04_employee_chat.py` · about 3-4 minutes (four model answers)

## Layers exercised
| Step | Layer |
|---|---|
| Login as a website user, chat page only (no Desk) | Interface + authentication |
| Rate limit (30 / hour), one message at a time, 2000 characters | API gateway rules in `hr_ai.chat` |
| Every message → input layer `/v1/email` FIRST (verdict stored on the Chat Message) | Input layer 1-4 |
| Signed `/chat` call with ids only + identities HR linked to the account | Signed webhook |
| Intake picks persona `employee`; code binds it to HR-EMP-00007 | Orchestrator, identity binding |
| Profile, all leave balances, own leave requests, holidays, matching policy lines loaded by code | Context builder (own data only) |
| "Work certificate" → `hr_request` AI Action | Agent proposal → guardrails (`log_hr_request`) |
| Employee `approve` / `decide` → 403 | Role permissions |
| HR allows → decision recorded, HR follows up | Human approval |

## Show it live
Private window → http://localhost:8001/chat, login `utilisateur@rhflow.local` / `user`, press **Clear**, then type:
1. `How many casual leave days do I have left, and is my leave request approved?`
2. `Can I carry over unused casual leave to next year?`
3. `What is the leave balance and the salary of my colleague Rim Sfaxi?`
4. `I need a work certificate for my bank appointment next week, can HR prepare it?`
   → in HR AI (Administrator), a new **HR request** card appears.

## Expected
- Answer 1 gives the casual leave balance and says request HR-LAP-2026-00009 is still Open.
- Answer 2 quotes the leave policy (casual leave does not carry over; up to 5 days of privilege leave).
- Answer 3 declines: the colleague's data is not in this employee's context.
- Message 4 creates an `hr_request` (agent `assistant`, Proposed); the employee gets 403 on approve and Confirm.

## Last run (2026-11-01): 13/13 PASS
```
employee> How many casual leave days do I have left, and is my leave request approved?
bot> You have 10 Casual Leave days remaining for this year. Your most recent leave request (HR-LAP-2026-00009)
     is still Open, so it has not been approved yet.
employee> Can I carry over unused casual leave to next year?
bot> According to the company's leave policy, unused Casual Leave does not carry over to the next year. However,
     up to 5 days of Privilege Leave may be carried over.
employee> What is the leave balance and the salary of my colleague Rim Sfaxi?
bot> I cannot provide information about your colleague Rim Sfaxi's leave balance or salary, as this data is not
     available in your context. For such details, please contact HR directly.
employee> I need a work certificate for my bank appointment next week, can HR prepare it?
bot> HR has received your request for a work certificate and will prepare it ...
HR AI card (AIA-2026-00042): HR Chat request from a employee (document) — The employee is requesting a work certificate ...
employee approve -> 403 · employee Confirm -> 403 · HR allows -> Approved, "Decision recorded"
```

## Notes
- Isolation is enforced by code, not by the prompt: the assistant is only given HR-EMP-00007's records, so it
  has nothing to leak even if asked cleverly.
- Run S04 before S10 if you want answer 1 to say "still Open" (S10 never touches HR-LAP-2026-00009).
