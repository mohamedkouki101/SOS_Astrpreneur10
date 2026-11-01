"""Bridge between HR Flow and the guardrails service (`guardrails/`, FastAPI, port 8100).

HR Flow keeps its own AI Action queue, decision page and Execution Gateway. It asks the
guardrails at the two moments that matter (guardrails/INTEGRATION.md, option A):

  proposal arrives  (api.propose_action, before the record is stored)  -> blocked = never queued
  right before acting (executor.execute, after the hash/token checks)  -> blocked = Failed

Fail closed: guardrails not configured, unreachable, or answering anything but a clear
"allowed" -> the action is refused. The guardrails check policy (default-deny tool allowlist
per agent, recipients must be known Frappe records), fairness (no protected attributes or
terms, justified scores) and token values (must match the person's Frappe record).

Site config (bench set-config, never in git): hr_ai_guardrails_url, hr_ai_guardrails_api_key,
hr_ai_guardrails_api_secret. That account must be an HR Manager, never an agent account.
"""

import json

import requests

CHECK_FIELDS = ("kind", "agent", "subject", "reference_doctype", "reference_name", "summary",
                "payload", "source_hashes", "token_map", "action_hash", "external_id", "run_id")
CONF_KEYS = ("hr_ai_guardrails_url", "hr_ai_guardrails_api_key", "hr_ai_guardrails_api_secret")


class GuardrailsRefused(Exception):
    """The guardrails said no, or could not be asked (fail closed)."""


def check_body(action: dict) -> dict:
    """The AI Action fields, in the format the guardrails accept (empty fields left out)."""
    body = {k: action.get(k) for k in CHECK_FIELDS if action.get(k) not in (None, "", {}, [])}
    for k in ("payload", "source_hashes", "token_map"):  # stored as JSON text in Frappe
        if isinstance(body.get(k), str):
            body[k] = json.loads(body[k])
    return body


def check(action: dict, conf) -> dict:
    """Dry run in the guardrails. Returns the verdict if allowed, raises GuardrailsRefused otherwise."""
    if not all(conf.get(k) for k in CONF_KEYS):
        raise GuardrailsRefused("Guardrails are not configured: action refused (fail closed)")
    url = conf.get("hr_ai_guardrails_url").rstrip("/") + "/agent-actions/check"
    auth = f"token {conf.get('hr_ai_guardrails_api_key')}:{conf.get('hr_ai_guardrails_api_secret')}"
    status, verdict = "no answer", None
    try:
        r = requests.post(url, json=check_body(action), headers={"Authorization": auth}, timeout=15)
        status = f"HTTP {r.status_code}"
        if r.status_code == 200:
            verdict = r.json()
    except (requests.RequestException, ValueError):
        pass
    if not isinstance(verdict, dict):
        # 401/403/422/503 or a network error: no verdict means no action.
        raise GuardrailsRefused(f"Guardrails gave no verdict ({status}): action refused (fail closed)")
    if verdict.get("allowed") is not True:
        rules = sorted({str(v.get("rule")) for v in verdict.get("violations") or [] if isinstance(v, dict)})
        raise GuardrailsRefused("Blocked by guardrails: " + (", ".join(rules) or "not allowed"))
    return verdict
