# agents/tools — tools and HR Core REST client

**Responsibility:** Tools agents may call, and the per-agent allowlist registry. `platform_client` reads via a scoped read-only token (one service user per agent) and submits proposals via `propose_action`.

**May depend on:** HR Core REST API only.
**Must never be depended on by:** `ingest/`.

**Rules:** No write credentials. No MariaDB connections. Tool outputs are DATA and are re-wrapped by the Context Builder. Keys from `.env`, never logged.
