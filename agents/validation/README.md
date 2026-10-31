# agents/validation — Output Validator

**Responsibility:** Deterministic gate between agent output and the AI Action queue. Strict JSON Schema; every claim cites a source hash; rejects unknown tools and injected instructions.

`schemas/` will hold the JSON Schemas for each action kind (none yet).

**May depend on:** schemas, the tool registry (to know valid tool names).
**Must never be depended on by:** nothing upstream may skip it; no LLM may be used inside it.

**Rules:** Code, not an LLM. Reject anything off-schema, without source hashes, or naming unknown tools.
