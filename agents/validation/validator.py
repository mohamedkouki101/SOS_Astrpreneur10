"""Output Validator - deterministic code, never an LLM.

A proposal is accepted only if:
- the agent is allowed to propose this kind (default-deny);
- the payload matches the strict schema (unknown fields rejected);
- it cites at least one source hash, and every cited hash was actually read in this run;
- every evidence quote really appears in the cited (masked) source text;
- free-text fields contain no injected instructions and no unmasked email addresses / phone numbers.
It also rejects tool calls to tools outside the agent's allowlist.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from pydantic import ValidationError as PydanticError

from agents.ingest.injection import rule_hits
from agents.validation.schemas import FREE_TEXT_FIELDS, PROPOSAL_KINDS_BY_AGENT, PROPOSAL_SCHEMAS

RAW_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
RAW_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
DATES = re.compile(r"\b\d{4}-\d{2}-\d{2}\b|\b\d{1,2}[:/.]\d{2}(?:[:/.]\d{2,4})?\b|\b(19|20)\d{2}\b")


class ValidationError(ValueError):
    pass


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def _free_text(obj: Any, key: str | None = None) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            out += _free_text(v, k)
    elif isinstance(obj, list):
        for v in obj:
            out += _free_text(v, key)
    elif isinstance(obj, str) and key in FREE_TEXT_FIELDS:
        out.append((key, obj))
    return out


def action_hash(kind: str, subject: str, payload: dict, source_hashes: list[str]) -> str:
    canonical = json.dumps({"kind": kind, "subject": subject, "payload": payload, "source_hashes": sorted(source_hashes)},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_tool_call(agent: str, tool_name: str, allowed: set[str]) -> None:
    if tool_name not in allowed:
        raise ValidationError(f"tool '{tool_name}' is not in the allowlist of agent '{agent}'")


def validate_proposal(agent: str, kind: str, subject: str, payload: dict, source_hashes: list[str],
                      sources: dict[str, Any]) -> dict:
    if kind not in PROPOSAL_KINDS_BY_AGENT.get(agent, set()):
        raise ValidationError(f"agent '{agent}' may not propose '{kind}'")
    schema = PROPOSAL_SCHEMAS.get(kind)
    if schema is None:
        raise ValidationError(f"unknown proposal kind '{kind}'")
    try:
        model = schema.model_validate(payload)
    except PydanticError as exc:
        raise ValidationError(f"payload off-schema: {exc.errors()[0]['msg']} at {exc.errors()[0]['loc']}") from exc
    clean = model.model_dump(mode="json")

    if not source_hashes:
        raise ValidationError("proposal cites no source hash")
    unknown = [h for h in source_hashes if h not in sources]
    if unknown:
        raise ValidationError(f"cited source hash not read in this run: {unknown[0][:12]}...")

    for ev in [e for entry in clean.get("entries", []) for e in entry.get("evidence", [])]:
        src = sources.get(ev["source_hash"])
        if src is None:
            raise ValidationError("evidence cites an unknown source hash")
        if _norm(ev["quote"]) not in _norm(src.text):
            raise ValidationError(f"evidence quote not found in source for skill '{ev['skill']}'")

    for field, text in _free_text(clean):
        if hits := rule_hits(text):
            raise ValidationError(f"injected instruction pattern in '{field}': {hits[0]}")
        no_dates = DATES.sub(" ", text)
        if field != "quote" and (RAW_EMAIL.search(text) or RAW_PHONE.search(no_dates)):
            raise ValidationError(f"unmasked contact data in '{field}'")

    return {"kind": kind, "subject": subject, "payload": clean, "source_hashes": sorted(set(source_hashes)),
            "action_hash": action_hash(kind, subject, clean, source_hashes)}
