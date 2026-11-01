"""Per-run context shared by the tools of one agent run (set with a ContextVar, never passed through the LLM).

Holds the agent name, its read-only client, the caller (for "own data only" tools), the sources
read during the run (hash -> masked text) and the evaluations/proposals produced. Tools look up
the caller and source hashes here, so the model cannot spoof them through tool arguments.
"""

from __future__ import annotations

import contextlib
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Iterator


@dataclass
class Source:
    source_id: str
    sha256: str
    text: str
    kind: str
    meta: dict = field(default_factory=dict)


@dataclass
class RunContext:
    agent: str
    client: Any = None                        # PlatformClient (or a fake in tests)
    caller_employee: str | None = None        # bound by the interface, never by the model
    caller_applicant: str | None = None       # HR Chat: the Job Applicant HR linked to the account
    chat_message: str | None = None           # HR Chat: the Chat Message this run answers
    persona: str | None = None                # HR Chat: "employee" / "applicant" (picked by intake, bound by code)
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    sources: dict[str, Source] = field(default_factory=dict)        # sha256 -> Source
    evaluations: dict[str, dict] = field(default_factory=dict)      # applicant -> evaluation
    proposals: list[dict] = field(default_factory=list)

    def add_source(self, source: Source) -> str:
        self.sources[source.sha256] = source
        return source.sha256

    def source_for(self, source_id: str) -> Source | None:
        return next((s for s in self.sources.values() if s.source_id == source_id), None)


_current: ContextVar[RunContext | None] = ContextVar("rhflow_run_context", default=None)


def current() -> RunContext:
    ctx = _current.get()
    if ctx is None:
        raise RuntimeError("No active agent run context")
    return ctx


@contextlib.contextmanager
def run_context(ctx: RunContext) -> Iterator[RunContext]:
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)


@contextlib.contextmanager
def child_context(agent: str, client: Any = None) -> Iterator[RunContext]:
    """Context for a specialist run started by the orchestrator: same caller, own client and sources."""
    parent = _current.get()
    ctx = RunContext(agent=agent, client=client, caller_employee=parent.caller_employee if parent else None)
    with run_context(ctx):
        yield ctx
