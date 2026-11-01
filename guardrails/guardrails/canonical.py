"""Canonical JSON + action hashing.

The same action must always produce the same bytes, otherwise the hash
(and so the approval token) would break for no reason, or two different
actions could be confused. Follows the spirit of RFC 8785 (JCS):
sorted keys, no whitespace, UTF-8, integral floats written as integers,
NaN/Infinity refused.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from .models import ProposedAction


class CanonicalizationError(ValueError):
    pass


def _normalize(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("NaN/Infinity cannot be hashed")
        return int(value) if value.is_integer() else value
    if isinstance(value, dict) or hasattr(value, "items"):
        out = {}
        for k, v in value.items():
            if not isinstance(k, str):
                raise CanonicalizationError("object keys must be strings")
            out[k] = _normalize(v)
        return out
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def action_body(action: ProposedAction) -> dict:
    """Everything that defines WHAT will be executed. All of it is hashed."""
    return {
        "action_id": action.action_id,
        "agent": action.agent,
        "tool": action.tool,
        "subject": {"doctype": action.subject.doctype, "name": action.subject.name},
        "payload": action.payload,
        "source_hashes": list(action.source_hashes),
        "model": action.model,
        "prompt_version": action.prompt_version,
    }


def action_hash(action: ProposedAction) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(action_body(action))).hexdigest()
