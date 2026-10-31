# agents/context — Context Builder

**Responsibility:** Assemble what goes to the LLM: trusted system prompt kept separate from data; pipeline output and tool outputs wrapped as data.

**May depend on:** `ingest/` output (provenance-tagged, PII-masked items), tool results.
**Must never be depended on by:** `ingest/`.

**Rules:** Never concatenate external content into the system prompt. One candidate per context window. No secrets in prompts.
