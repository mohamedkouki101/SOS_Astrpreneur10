# Limitations

What is not built, simplified for the hackathon, or known to behave imperfectly. State as of 2026-11-01,
branch `main`. Overview of what *is* built: [`README.md`](README.md).

The security design does not depend on any single detector being perfect: agents have no write rights, their
output is validated by code, the guardrails check every action twice, and a human approves. The items below
are gaps in coverage, not ways for an agent to act alone.

---

## 1. Repository state

| Item | Detail |
|---|---|
| Everything is in the repository | Platform setup (`platform/`), Frappe HR source (`hrms/`), our apps (`hr_ai/`, `hrflow_theme/`), services, agents, tests and the 12 scenarios. 796 automated tests pass (67 + 362 + 367). |
| Clean-install path not timed end to end | The stack was built and tested step by step on the team's machine (image `custom:N` from `platform/hr.yml`). `scripts/setup_stack.py` and `platform/build-images.sh` package those same steps; a full rebuild from an empty Docker host was not re-run before submission. `apps.json` pins ERPNext / HRMS to `v16.53.0`; Frappe follows the `version-16` branch. |
| Setup wizard is manual | Company **Bara3am** must be created in the Frappe setup wizard before the seed runs. |
| Two masking pipelines | `input-security-layer/` (service, tokens `<PERSON_1>`) and `agents/ingest/` (local fallback, tokens `[PERSON_1]`). `INPUT_LAYER=service` is the intended mode; the local one is kept for offline tests. The gateway handles both formats. |

## 2. Input security layer (`input-security-layer/`)

- **No IMAP worker.** Emails are pulled from Mailpit's HTTP API by `hr_ai` (scheduler, every minute) and forwarded to `/v1/email`. A real mailbox (IMAP, Microsoft 365, Gmail) is not connected.
- **Rules only, no classifier.** The injection scanner uses FR + EN regex rules plus structural signals. No ML classifier (Prompt Guard is gated and was not used). Paraphrased or novel instructions can pass as `clean`.
- **One visible rule = `suspicious`, not `quarantined`.** A single visible "ignore the previous instructions" scores 4000 bp: the document is released to the agent with the phrase replaced by `[REMOVED: suspected instruction]` and a flag. Two or more rules, or any rule in hidden text, quarantine it.
- **Some poisoned CVs are not quarantined.** In scenario 03, a tiny-font payload with no matching rule is removed by the sanitizer and the CV is `clean`. The payload never reaches the model, but HR sees no quarantine.
- **No OCR.** A scanned CV (image-only PDF) yields no text and is flagged "no readable text"; it is not read.
- **No malware scanner.** Files are type-checked, size-limited, macro-checked and parsed in a child process, but not scanned by an antivirus.
- **Languages:** English and French only. No Arabic recognizers or models.
- **PII detection is statistical.** Presidio + spaCy can miss unusual names or formats. The agents re-mask names from the trusted Frappe record as a second pass, but a missed identifier in free text can reach the model.
- **Memory/CPU limits on the extraction child are Linux-only.** On Windows only the timeout applies. The Docker image (the target) is Linux.
- **No audit events from this service.** Decisions (received, quarantined, released, rejected) are stored in the inspection reports and on the Frappe records, but there is no separate append-only audit stream yet.
- **Quarantines are not reported to the guardrails monitor.** `POST /input-flags` exists in the guardrails, but nothing calls it yet. HR sees quarantines in the **Security checks** tab.
- **Keys are static.** Four role-separated API keys and two HMAC keys from `.env`, no rotation, no secrets manager.

## 3. Agents (`agents/`)

- **Demo scope:** Orchestrator, Recruitment, Comms, HR Assistant, Leave, and the chat Operator. Onboarding, Payroll, Performance and Conflict are minimal stubs, and the policy engine denies their proposals by default.
- **Local LLM is slow:** 3–10 s per step, 20–60 s per run; ranking one opening (~16 CVs) takes several minutes. Rankings are prepared before the demo.
- **Model quality:** a 14B local model can pick a wrong tool or write a weak draft. Code limits what it can do (allowlist, schemas, budgets); a human reviews every proposal.
- **Calendar and policies are files** (JSON free slots, policy documents), not a real calendar or document system.

## 4. Guardrails, approval and execution (`guardrails/`, `hr_ai/`)

- **Refused proposals leave no central audit entry.** `hr_ai` has no audit or monitor of its own: that logic lives in the guardrails service. In the wired mode `hr_ai` calls the guardrails' dry-run check, so a refusal is returned to the caller and logged on the agent side (`var/audit.jsonl`), but a direct API call that is refused leaves no trace.
- **The guardrails' own queue, audit chain and monitor are built and tested but not on the main path.** The decision trail in the demo is the AI Action history in Frappe (who approved, when), plus the agent-side hash-chained trail.
- **HR AI card hides placeholders.** Frappe's HTML filter strips `<SENDER>` and `<PERSON_1>` from the card summary (they look like tags), so the card shows `To: ` / `Hello ,`. The payload and the sent email are correct; show **Details / edit → Payload** in the demo. The filter must stay on: the summary is model output.
- **Decision-only kinds** (ranking, digest, leave decision) record HR's decision but trigger no automatic write; HR follows up by hand.
- **Approval key** (`hr_ai_approval_secret`) lives in Frappe site config; no key management or rotation.
- **At-most-once execution:** a timeout during a write marks the action Failed / outcome unknown and is never retried automatically.
- **Monitor alerts** go to the console and the database; there is no email / chat notification channel.

## 5. Platform and deployment

- **No API gateway, SSO or MFA.** HR users log in with Frappe's own accounts. The default `Administrator` / `admin` login is used in the demo and must be changed on any shared machine.
- **Test mode only:** every outgoing email goes to `hr_ai_test_recipient` (Mailpit). Real sending needs an outgoing Email Account and turning test mode off.
- **Single machine, no TLS.** Services talk over plain HTTP on localhost / the Docker network. The input layer binds to `127.0.0.1` in compose; the guardrails example binds to `0.0.0.0` and should be firewalled.
- **`hr_ai` must be built into the Frappe image** (10–15 min rebuild); changes are not hot-reloaded.
- **Licensing:** Frappe HR (`hrms/`) and ERPNext are GPL v3 and are redistributed unmodified with their licence files; `platform/` is based on frappe_docker (MIT). The licence of our own Frappe apps relative to the GPL was not reviewed by a lawyer.

## 6. Data

- All data is fictional (CVs, emails, employees, job openings). No real personal data was used.
- Evidence store contents (raw files, inspection reports, PII token maps) are local runtime data and are not tracked in git.
- The ranking was evaluated on a small fictional set (scenario 03), not on a representative sample; no bias audit on real hiring data.
