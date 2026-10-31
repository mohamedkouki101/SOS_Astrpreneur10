"""Step, tool-call, token and proposal budgets for one agent run."""

from __future__ import annotations

from dataclasses import dataclass

from agents.config import get_settings


@dataclass(frozen=True)
class Budget:
    max_tool_calls: int
    max_steps: int
    max_tokens: int
    max_proposals: int

    @classmethod
    def default(cls, **overrides) -> Budget:
        s = get_settings()
        values = dict(max_tool_calls=s.max_tool_calls, max_steps=s.max_steps,
                      max_tokens=s.max_tokens, max_proposals=s.max_proposals)
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)

    def exceeded(self, *, steps: int, tool_calls: int, tokens: int) -> str | None:
        if steps >= self.max_steps:
            return f"step budget reached ({self.max_steps})"
        if tool_calls >= self.max_tool_calls:
            return f"tool-call budget reached ({self.max_tool_calls})"
        if tokens >= self.max_tokens:
            return f"token budget reached ({self.max_tokens})"
        return None
