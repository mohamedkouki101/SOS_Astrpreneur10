"""propose_action: the ONLY output path of every agent.

A proposal is validated (Output Validator) and stored as an AI Action with status "Proposed".
Nothing is executed here. Every proposal is kept in var/outbox/proposals.jsonl; with
RHFLOW_PROPOSE_ENABLED=1 it is also POSTed to hr_ai.api.propose_action, which re-checks it and
stores it as an "AI Action" (status Proposed) in HR Flow for HR to approve or reject.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import httpx

from agents.config import get_settings
from agents.context.run_context import current
from agents.ingest import pipeline
from agents.orchestrator import audit
from agents.validation.validator import ValidationError, validate_proposal


def _outbox_path():
    p = get_settings().var_dir / "outbox" / "proposals.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def propose_action(kind: str, subject: str, payload: dict, source_hashes: list[str]) -> dict:
    ctx = current()
    if len(ctx.proposals) >= get_settings().max_proposals:
        audit.log("proposal_rejected", agent=ctx.agent, run_id=ctx.run_id, kind=kind, reason="volume limit")
        return {"status": "REJECTED", "reason": "proposal volume limit for this run reached"}
    try:
        checked = validate_proposal(ctx.agent, kind, subject, payload, source_hashes, ctx.sources)
    except ValidationError as exc:
        audit.log("proposal_rejected", agent=ctx.agent, run_id=ctx.run_id, kind=kind, reason=str(exc))
        return {"status": "REJECTED", "reason": str(exc)}

    action = {
        "id": f"AIA-{uuid.uuid4().hex[:10]}",
        "status": "Proposed",
        "agent": ctx.agent,
        "run_id": ctx.run_id,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **checked,
    }
    if get_settings().propose_to_rhflow:
        try:
            action["rhflow_name"] = _submit(ctx.agent, action)
        except (httpx.HTTPError, RuntimeError) as exc:
            audit.log("proposal_submit_failed", agent=ctx.agent, run_id=ctx.run_id, action_id=action["id"],
                      error=str(exc)[:300])
            return {"status": "ERROR", "reason": "HR Flow refused or could not store the proposal"}
    with _outbox_path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(action, ensure_ascii=False) + "\n")
    ctx.proposals.append(action)
    audit.log("proposal_created", agent=ctx.agent, run_id=ctx.run_id, action_id=action["id"], kind=kind,
              action_hash=action["action_hash"], source_hashes=action["source_hashes"])
    return {"status": "Proposed", "action_id": action.get("rhflow_name") or action["id"],
            "action_hash": action["action_hash"],
            "note": "Waiting for human approval. Nothing has been sent or changed."}


def _token_values(action: dict) -> dict:
    """Real values for the pseudonyms used in this proposal. Sent only to HR Flow (the system of
    record, permlevel-1 field) so the Execution Gateway can de-tokenise; never shown to the model."""
    text = action["subject"] + json.dumps(action["payload"], ensure_ascii=False)
    store = pipeline.token_store()
    found = {t: store.original(t) for t in set(re.findall(r"\[[A-Z]+_\d+\]", text))}
    return {t: v for t, v in found.items() if v is not None}


def _submit(agent: str, action: dict) -> str:
    s = get_settings()
    creds = s.credentials(agent)
    if not creds:
        raise RuntimeError(f"no HR Flow API credentials for agent '{agent}'")
    resp = httpx.post(f"{s.rhflow_url}/api/method/hr_ai.api.propose_action",
                      headers={"Authorization": f"token {creds[0]}:{creds[1]}"},
                      json={"action": {**action, "token_values": _token_values(action)}}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    return resp.json()["message"]["name"]


def get_action(action_id: str) -> dict | None:
    path = _outbox_path()
    if not path.is_file():
        return None
    for line in path.read_text(encoding="utf-8").splitlines():
        a = json.loads(line)
        if action_id in (a["id"], a.get("rhflow_name")):
            return {k: a[k] for k in ("id", "status", "kind", "agent", "created", "subject")}
    return None


def list_actions(status: str | None = None) -> list[dict]:
    path = _outbox_path()
    if not path.is_file():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return [r for r in rows if status is None or r["status"] == status]
