# tests

**Responsibility:** Checks for each build-plan task and one test per guardrail.

| Path | Content |
|---|---|
| `guardrails/` | injection, bypass, disparity, duplicate webhook (placeholders) |
| `hr_ai/` | `hr_ai` app tests (TODO) |
| `agents/` | agent service tests (TODO) |

Test runner/commands: TODO (CLAUDE.md §8).

**Rules:** External content goes through stages 1–4 even in tests. Never loosen Policy Engine defaults to pass a test. Fictional data only.
