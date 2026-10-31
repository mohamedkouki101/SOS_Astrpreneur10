"""Data contract between the agents and the guardrails service.

An agent never acts. It sends a ProposedAction; the guardrails decide.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class RecordRef:
    """A pointer to a Frappe record (e.g. Job Applicant HR-APP-0001).

    Agents reference people by record, never by raw email/phone, so the
    real address is resolved by the Execution Gateway from Frappe itself.
    """

    doctype: str
    name: str

    @classmethod
    def parse(cls, value: Any) -> "RecordRef | None":
        """Return a RecordRef from a {"doctype", "name"} mapping, else None."""
        if isinstance(value, RecordRef):
            return value
        if (
            isinstance(value, Mapping)
            and set(value.keys()) == {"doctype", "name"}
            and isinstance(value["doctype"], str)
            and isinstance(value["name"], str)
            and value["doctype"]
            and value["name"]
        ):
            return cls(value["doctype"], value["name"])
        return None


@dataclass(frozen=True)
class Violation:
    """One reason a guardrail refused an action. `rule` is a stable code."""

    rule: str
    reason: str


@dataclass(frozen=True)
class ProposedAction:
    action_id: str
    agent: str
    tool: str
    subject: RecordRef
    payload: Mapping[str, Any]
    source_hashes: tuple[str, ...] = field(default_factory=tuple)
    model: str | None = None           # e.g. LLM model id that produced the proposal
    prompt_version: str | None = None  # version of the agent's system prompt
