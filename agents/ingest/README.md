# agents/ingest — Untrusted Input Zone pipeline

**Responsibility:** Fixed, ordered 4-stage pipeline for resumes (PDF/DOCX) and emails (EML), plus mail ingestion.

| File | Stage |
|---|---|
| `mail_ingester.stub` | Source: IMAP pull or HR Core Email Account + webhook (TODO verify) |
| `sanitizer.stub` | 1 Sanitizer: type/size allowlist, malware scan (TODO), sandboxed parser, strip macros/links/hidden text |
| `pii.stub` | 2 PII masking (Presidio per CLAUDE.md): tokens for names/emails/phones; remove age, gender, photo, nationality |
| `injection.stub` | 3 Injection scanner: rules + classifier (TODO); quarantine + alert |
| `provenance.stub` | 4 Provenance tagger: source ID, SHA-256, candidate ID, trust=untrusted |

**May depend on:** nothing in `context/`, `orchestrator/` or `agents/` (it runs upstream of them).
**Must never be depended on by:** anything that bypasses it; only `context/` consumes its output.

**Rules:** Stages run in order and are never skipped, including in tests. PII masked before any scoring/LLM call. Raw originals stored in HR Core; token map storage is an open question.
