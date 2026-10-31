"""Policy Engine: deterministic, default-deny.

An action is allowed only if an explicit rule allows it:
  1. the agent is known,
  2. the tool is on that agent's allowlist,
  3. the tool is defined in the policy,
  4. every constraint configured for that tool passes.
Anything missing, unknown or malformed is denied.

The engine is pure: no network, no database. Usage history (for rate
limits) is injected through the UsageCounter protocol.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .models import ProposedAction, RecordRef, Violation

# Constraint keys a tool may declare. Anything else in the config is a
# typo and must fail loudly: a misspelled limit would silently not apply.
KNOWN_TOOL_KEYS = {
    "description",
    "subject_doctypes",
    "max_recipients",
    "recipient_doctypes",
    "recipient_must_be_subject",
    "rate_limit",
}


class PolicyConfigError(ValueError):
    """The policy file is malformed. The service must refuse to start."""


@dataclass(frozen=True)
class Decision:
    allowed: bool
    policy_version: str
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def reasons(self) -> list[str]:
        return [f"{v.rule}: {v.reason}" for v in self.violations]


class UsageCounter(Protocol):
    def count(self, agent: str, tool: str, window_seconds: int) -> int:
        """Number of actions this agent proposed with this tool in the window."""
        ...


class NoUsage:
    """Counter that reports zero usage. For tools without rate limits/tests."""

    def count(self, agent: str, tool: str, window_seconds: int) -> int:
        return 0


@dataclass(frozen=True)
class RateLimit:
    max: int
    window_seconds: int


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    subject_doctypes: frozenset[str] | None = None
    max_recipients: int | None = None
    recipient_doctypes: frozenset[str] | None = None
    recipient_must_be_subject: bool = False
    rate_limit: RateLimit | None = None


class PolicyEngine:
    def __init__(self, config: Mapping[str, Any]):
        self.version, self.agent_tools, self.tools = _parse_config(config)

    @classmethod
    def from_file(cls, path: str | Path) -> "PolicyEngine":
        with open(path, encoding="utf-8") as fh:
            return cls(json.load(fh))

    def evaluate(
        self, action: ProposedAction, usage: UsageCounter | None = None
    ) -> Decision:
        usage = usage or NoUsage()
        violations: list[Violation] = []

        allowed_tools = self.agent_tools.get(action.agent)
        if allowed_tools is None:
            return self._deny("unknown_agent", f"agent '{action.agent}' is not registered")

        if action.tool not in allowed_tools:
            return self._deny(
                "tool_not_allowed",
                f"agent '{action.agent}' may not use tool '{action.tool}'",
            )

        tool = self.tools.get(action.tool)
        if tool is None:  # allowlisted but undefined -> config gap, deny
            return self._deny("unknown_tool", f"tool '{action.tool}' has no policy")

        if not isinstance(action.subject, RecordRef):
            return self._deny("bad_subject", "subject must be a record reference")

        if tool.subject_doctypes is not None and action.subject.doctype not in tool.subject_doctypes:
            violations.append(Violation(
                "subject_doctype",
                f"tool '{tool.name}' cannot act on '{action.subject.doctype}'",
            ))

        if tool.max_recipients is not None:
            violations.extend(self._check_recipients(tool, action))

        if tool.rate_limit is not None:
            used = usage.count(action.agent, action.tool, tool.rate_limit.window_seconds)
            if used >= tool.rate_limit.max:
                violations.append(Violation(
                    "rate_limit",
                    f"{used} '{tool.name}' actions in the last "
                    f"{tool.rate_limit.window_seconds}s (max {tool.rate_limit.max})",
                ))

        return Decision(not violations, self.version, tuple(violations))

    def _check_recipients(self, tool: ToolPolicy, action: ProposedAction) -> list[Violation]:
        raw = action.payload.get("recipients")
        if not isinstance(raw, list) or not raw:
            return [Violation("recipients_missing", "payload.recipients must be a non-empty list")]

        out: list[Violation] = []
        if len(raw) > tool.max_recipients:
            out.append(Violation(
                "too_many_recipients",
                f"{len(raw)} recipients (max {tool.max_recipients})",
            ))

        for item in raw:
            ref = RecordRef.parse(item)
            if ref is None:
                # Raw addresses are refused: the gateway resolves addresses
                # from Frappe, so an injected "send to x@evil.com" cannot work.
                out.append(Violation(
                    "recipient_not_a_record",
                    "recipients must be {doctype, name} record references, not addresses",
                ))
                continue
            if tool.recipient_doctypes is not None and ref.doctype not in tool.recipient_doctypes:
                out.append(Violation(
                    "recipient_doctype",
                    f"cannot send to a '{ref.doctype}' record",
                ))
            if tool.recipient_must_be_subject and ref != action.subject:
                out.append(Violation(
                    "recipient_not_subject",
                    f"recipient {ref.doctype}/{ref.name} is not the action subject",
                ))
        return out

    def _deny(self, rule: str, reason: str) -> Decision:
        return Decision(False, self.version, (Violation(rule, reason),))


def _parse_config(config: Mapping[str, Any]):
    if not isinstance(config, Mapping):
        raise PolicyConfigError("policy must be a JSON object")

    version = config.get("version")
    if not isinstance(version, str) or not version:
        raise PolicyConfigError("policy.version is required")

    agents = config.get("agents")
    tools_cfg = config.get("tools")
    if not isinstance(agents, Mapping) or not isinstance(tools_cfg, Mapping):
        raise PolicyConfigError("policy.agents and policy.tools must be objects")

    tools: dict[str, ToolPolicy] = {}
    for name, spec in tools_cfg.items():
        if not isinstance(spec, Mapping):
            raise PolicyConfigError(f"tool '{name}' must be an object")
        unknown = set(spec) - KNOWN_TOOL_KEYS
        if unknown:
            raise PolicyConfigError(f"tool '{name}' has unknown keys: {sorted(unknown)}")
        tools[name] = ToolPolicy(
            name=name,
            subject_doctypes=_str_set(spec, "subject_doctypes", name),
            max_recipients=_pos_int(spec, "max_recipients", name),
            recipient_doctypes=_str_set(spec, "recipient_doctypes", name),
            recipient_must_be_subject=bool(spec.get("recipient_must_be_subject", False)),
            rate_limit=_rate_limit(spec, name),
        )
        if tools[name].recipient_doctypes and tools[name].max_recipients is None:
            raise PolicyConfigError(f"tool '{name}': recipient rules need max_recipients")

    agent_tools: dict[str, frozenset[str]] = {}
    for agent, spec in agents.items():
        if not isinstance(spec, Mapping) or not isinstance(spec.get("tools"), list):
            raise PolicyConfigError(f"agent '{agent}' must have a 'tools' list")
        missing = [t for t in spec["tools"] if t not in tools]
        if missing:
            raise PolicyConfigError(f"agent '{agent}' references undefined tools: {missing}")
        agent_tools[agent] = frozenset(spec["tools"])

    return version, agent_tools, tools


def _str_set(spec, key, tool):
    if key not in spec:
        return None
    val = spec[key]
    if not isinstance(val, list) or not all(isinstance(x, str) and x for x in val):
        raise PolicyConfigError(f"tool '{tool}': {key} must be a list of strings")
    return frozenset(val)


def _pos_int(spec, key, tool):
    if key not in spec:
        return None
    val = spec[key]
    if not isinstance(val, int) or isinstance(val, bool) or val < 1:
        raise PolicyConfigError(f"tool '{tool}': {key} must be a positive integer")
    return val


def _rate_limit(spec, tool):
    if "rate_limit" not in spec:
        return None
    rl = spec["rate_limit"]
    if not isinstance(rl, Mapping) or set(rl) != {"max", "window_seconds"}:
        raise PolicyConfigError(f"tool '{tool}': rate_limit needs exactly max and window_seconds")
    return RateLimit(_pos_int(rl, "max", tool), _pos_int(rl, "window_seconds", tool))
