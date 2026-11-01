"""Doc events from HR Flow handled by the agent service (asynchronously, one at a time).

inbound_email.received: a mail released by the input security layer -> the Comms agent reads it and,
if it needs an answer, proposes a reply (AI Action "Proposed", HR approves in RH AI).
"""

from __future__ import annotations

import re
from typing import Any

from agents.agents.base import run_agent
from agents.agents.specs import COMMS
from agents.context.run_context import RunContext

INBOUND = re.compile(r"^IN-\d{4}-\d{5,}$")


class EventError(Exception):
    pass


def handle_event(event: dict, *, llm: Any = None) -> dict:
    if event.get("type") != "inbound_email.received":
        raise EventError(f"unknown event type {event.get('type')!r}")
    name = event.get("name", "")
    if not INBOUND.fullmatch(name):
        raise EventError("invalid Inbound Email name")
    task = (f"A new email arrived in the HR inbox: {name}. Read it with read_email (email_id '{name}'). "
            "If it needs an answer, look up the facts you need and draft ONE reply with propose_email_reply. "
            "If it is spam, quarantined or needs no answer, do not propose anything and say why in one sentence.")
    res = run_agent(COMMS, task, ctx=RunContext(agent="comms"), llm=llm)
    return {"answer": res["answer"][:500], "proposals": [p.get("rhflow_name") or p["id"] for p in res["proposals"]],
            "run_id": res["run_id"], "stopped": res.get("stopped")}
