"""Helpers shared by all tools.

Rules applied here:
- Records read from HR Flow are registered as sources (SHA-256 of their JSON) so proposals can cite them.
- Tools return IDs and job-related fields only: never names, emails, phones, birth dates or addresses
  (those are personal data and must not reach the LLM unmasked).
- Free-text fields written by people (leave reasons, grievances, feedback) go through the ingest pipeline.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import re
from pathlib import Path
from typing import Any, Callable

from agents.config import get_settings
from agents.context.run_context import Source, current
from agents.ingest import pipeline, provenance
from agents.tools.platform_client import PlatformClient, PlatformError

PERSONAL_FIELDS = {
    "employee_name", "first_name", "middle_name", "last_name", "applicant_name", "email_id", "phone_number",
    "personal_email", "company_email", "cell_number", "date_of_birth", "gender", "marital_status",
    "current_address", "permanent_address", "image", "employee_image", "raised_by_name", "reviewer_name",
    "leave_approver_name", "user_id", "user", "passport_number", "bank_ac_no", "iban", "blood_group",
}


def ref(kind: str, real: str) -> str:
    """Pseudonymous, stable reference for an identifier that is itself personal data
    (v16 names Job Applicant records by email address). The model only ever sees the reference."""
    store = pipeline.token_store()
    tok = store.token(kind, real)
    store.save()
    return tok


def unref(value: str) -> str:
    """Resolve a reference like [APPLICANT_3] back to the real record name (code only)."""
    if re.fullmatch(r"\[[A-Z]+_\d+\]", value or ""):
        real = pipeline.token_store().original(value)
        if real is None:
            raise ValueError(f"unknown reference {value}")
        return real
    return value


def client() -> PlatformClient:
    ctx = current()
    if ctx.client is None:
        ctx.client = PlatformClient(ctx.agent)
    return ctx.client


def strip_personal(record: dict) -> dict:
    return {k: v for k, v in record.items() if k not in PERSONAL_FIELDS and not k.startswith("_")}


def register_record(source_id: str, data: Any) -> str:
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return current().add_source(Source(source_id=source_id, sha256=provenance.sha256(text), text=text, kind="record"))


def masked_text(source_id: str, text: str, known_pii: dict[str, str] | None = None) -> dict:
    """Run a free-text field through the 4-stage pipeline. Returns text or a quarantine notice."""
    if not (text or "").strip():
        return {"status": "ok", "text": ""}
    res = pipeline.ingest_text(source_id, re.sub(r"<[^>]+>", " ", text), known_pii=known_pii)
    if res.status != "ok":
        return {"status": "QUARANTINED", "reason": res.injection_hits or res.findings}
    current().add_source(Source(source_id=source_id, sha256=res.sha256, text=res.text, kind="text"))
    return {"status": "ok", "text": res.text, "source_hash": res.sha256}


def safe_tool(fn: Callable) -> Callable:
    """Turn platform/data errors into a short error result instead of crashing the agent run."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except PlatformError as exc:
            return {"error": str(exc)}
        except (KeyError, ValueError, FileNotFoundError) as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}

    return wrapper


def today() -> dt.date:
    return dt.date.today()


def dataset(*parts: str) -> Path:
    return get_settings().dataset_dir.joinpath(*parts)


# ------------------------------------------------------------------ policies (simple keyword RAG)
def search_policy_snippets(query: str, limit: int = 5) -> list[dict]:
    words = {w for w in re.findall(r"[a-z]{3,}", query.lower())}
    out = []
    for path in sorted(dataset("policies").glob("*.md")):
        text = path.read_text(encoding="utf-8")
        sha = register_record(f"policy/{path.name}", text)
        for line in text.splitlines():
            line = line.strip("-# ").strip()
            if not line:
                continue
            score = sum(1 for w in words if w in line.lower()) + (1 if any(w in path.stem for w in words) else 0)
            if score:
                out.append({"policy": path.stem, "text": line, "score": score, "source_hash": sha})
    out.sort(key=lambda r: -r["score"])
    return [{k: v for k, v in r.items() if k != "score"} for r in out[:limit]]


def leave_balance(employee: str, leave_type: str, year: int | None = None) -> dict:
    year = year or today().year
    rows = client().get_list(
        "Leave Ledger Entry",
        filters=[["employee", "=", employee], ["leave_type", "=", leave_type], ["docstatus", "=", 1],
                 ["is_expired", "=", 0], ["from_date", ">=", f"{year}-01-01"], ["to_date", "<=", f"{year}-12-31"]],
        fields=["leaves", "transaction_type"], limit=500,
    )
    allocated = sum(r["leaves"] for r in rows if r["leaves"] > 0)
    taken = -sum(r["leaves"] for r in rows if r["leaves"] < 0)
    result = {"employee": employee, "leave_type": leave_type, "year": year,
              "allocated": allocated, "taken": taken, "remaining": allocated - taken}
    result["source_hash"] = register_record(f"Leave Ledger/{employee}/{leave_type}/{year}", result)
    return result
