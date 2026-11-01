"""Whitelisted API. POST /api/method/hr_ai.api.propose_action

The ONLY way agents put anything into RH Flow. Agents have no create/write permission on any
DocType; this method checks the caller's agent role, re-validates the proposal, recomputes the
action hash server-side and always stores it as "Proposed". It never executes anything.
"""

import json
import re

import frappe

from hr_ai.approval import canonical_hash

KINDS_BY_AGENT = {
    "recruitment": {"ranking", "applicant_status", "interview"},
    "comms": {"email_reply", "digest"},
    "leave": {"leave_decision"},
    "onboarding": {"onboarding_tasks", "welcome_email"},
    "payroll": {"payroll_reply"},
    "performance": {"review_summary"},
    "conflict": {"grievance_triage"},
}
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"\[[A-Z]+_\d+\]")


def _agent_of_caller(claimed: str) -> str:
    role = f"RH Flow Agent {claimed.capitalize()}"
    if claimed not in KINDS_BY_AGENT or role not in frappe.get_roles():
        frappe.throw("Only the matching agent service user may propose actions", frappe.PermissionError)
    return claimed


def _summary(kind: str, p: dict) -> str:
    if kind == "email_reply":
        return f"To: {p['to']}\nSubject: {p['subject']}\n\n{p['body']}"
    if kind == "welcome_email":
        return f"Employee: {p['employee']}\nSubject: {p['subject']}\n\n{p['body']}"
    if kind == "ranking":
        lines = [f"Opening {p['opening']}"] + [
            f"#{e['rank']}  {e['applicant']}  score {e['score']}  -  {e['justification']}" for e in p["entries"]]
        if p.get("quarantined"):
            lines.append(f"Quarantined (security): {', '.join(p['quarantined'])}")
        return "\n".join(lines)
    if kind == "digest":
        return "\n".join(f"[{i['priority'].upper()}] {i['category']}: {i['summary']} ({i['email_id']})" for i in p["items"])
    if kind == "leave_decision":
        return f"{p['decision']} - {p['application']}\nReason: {p['reason']}"
    if kind == "interview":
        return (f"{p['interview_type']} with {p['applicant']}\n"
                f"{p['date']}  {p['from_time']} - {p['to_time']}")
    if kind == "payroll_reply":
        return f"Employee: {p['employee']}\n\n{p['body']}"
    return json.dumps(p, indent=1, ensure_ascii=False)


def _token_values(raw, subject: str, payload: dict) -> dict:
    """Real values for the pseudonyms used in THIS proposal only (stored in a permlevel-1 field,
    used only by the Execution Gateway). Tokens not present in the proposal are dropped."""
    if not isinstance(raw, dict):
        return {}
    used = set(TOKEN.findall(subject + json.dumps(payload, ensure_ascii=False)))
    return {k: str(v)[:200] for k, v in raw.items() if k in used and isinstance(v, str)}


SUBJECT_DOCTYPES = {"email": "Inbound Email"}


def _reference(subject: str, tokens: dict):
    doctype, _, name = (subject or "").partition("/")
    doctype = SUBJECT_DOCTYPES.get(doctype, doctype)
    name = tokens.get(name, name)
    if doctype and name and frappe.db.exists("DocType", doctype) and frappe.db.exists(doctype, name):
        return doctype, name
    return None, None


@frappe.whitelist(methods=["POST"])
def propose_action(action):
    action = frappe.parse_json(action)
    agent = _agent_of_caller(str(action.get("agent", "")))
    kind = action.get("kind")
    if kind not in KINDS_BY_AGENT[agent]:
        frappe.throw(f"Agent '{agent}' may not propose '{kind}'", frappe.PermissionError)

    payload, subject = action.get("payload"), str(action.get("subject", ""))[:140]
    source_hashes = action.get("source_hashes") or []
    if not isinstance(payload, dict) or not source_hashes or not all(HEX64.match(h) for h in source_hashes):
        frappe.throw("Proposal must have a JSON payload and at least one valid source hash")
    action_hash = canonical_hash(kind, subject, payload, source_hashes)
    if action.get("action_hash") and action["action_hash"] != action_hash:
        frappe.throw("action_hash does not match the payload")

    external_id = str(action.get("id", ""))[:40]
    if external_id and (existing := frappe.db.get_value("AI Action", {"external_id": external_id}, "name")):
        return {"name": existing, "status": frappe.db.get_value("AI Action", existing, "status"), "duplicate": True}

    # No id from the agent: make one now and store it, so the proposal check and the
    # pre-execution check send the guardrails the same external_id.
    external_id = external_id or f"AIA-{frappe.generate_hash(length=10)}"

    tokens = _token_values(action.get("token_values"), subject, payload)
    ref_dt, ref_name = _reference(subject, tokens)
    record = {
        "doctype": "AI Action",
        "kind": kind,
        "agent": agent,
        "subject": subject,
        "reference_doctype": ref_dt,
        "reference_name": ref_name,
        "payload": json.dumps(payload, ensure_ascii=False, indent=1),
        "summary": _summary(kind, payload),
        "source_hashes": json.dumps(sorted(source_hashes)),
        "action_hash": action_hash,
        "external_id": external_id,
        "run_id": str(action.get("run_id", ""))[:40],
        "status": "Proposed",
        "token_map": json.dumps(tokens, ensure_ascii=False, indent=1) if tokens else None,
    }
    _ask_guardrails(record)  # blocked proposals never reach the HR queue
    doc = frappe.get_doc(record)
    doc.flags.ignore_permissions = True
    doc.flags.from_propose_action = True
    doc.insert()
    return {"name": doc.name, "status": doc.status}


def _ask_guardrails(record: dict):
    """Policy, fairness and token checks in the guardrails service. Fail closed."""
    from hr_ai.guardrails_client import GuardrailsRefused, check
    try:
        check(record, frappe.conf)
    except GuardrailsRefused as exc:
        frappe.throw(str(exc), frappe.PermissionError)


# ---------------------------------------------------------------- RH AI decision page (Desk app)

REVIEW_ROLES = {"HR Manager", "HR User", "System Manager"}
DECIDER_ROLES = {"HR Manager", "System Manager"}
REVIEW_FIELDS = ["name", "status", "kind", "agent", "subject", "summary", "reference_doctype", "reference_name",
                 "creation", "approver", "approved_on", "decision_note", "execution_result", "executed_on",
                 "result_doctype", "result_name"]


def check_app_permission() -> bool:
    """Show the RH AI tile on the /desk apps screen only to HR reviewers."""
    return frappe.session.user == "Administrator" or bool(REVIEW_ROLES & set(frappe.get_roles()))


@frappe.whitelist(methods=["GET"])
def review_queue(status: str = "Proposed", agent: str | None = None, limit: int = 50):
    """AI Actions for the decision page. Permission-aware (frappe.get_list).
    Real values behind pseudonyms are returned only to roles that may decide (permlevel-1 readers)."""
    if not check_app_permission():
        frappe.throw("Not permitted", frappe.PermissionError)
    statuses = {"Proposed", "Approved", "Rejected", "Executed", "Failed"}
    filters = {"status": status} if status in statuses else {}
    if agent and agent in KINDS_BY_AGENT:
        filters["agent"] = agent
    rows = frappe.get_list("AI Action", filters=filters, fields=REVIEW_FIELDS, order_by="creation desc",
                           limit_page_length=min(int(limit), 200))
    can_decide = bool(DECIDER_ROLES & set(frappe.get_roles()))
    if can_decide and rows:
        extra = {r[0]: r[1:] for r in frappe.get_list("AI Action", filters={"name": ["in", [r.name for r in rows]]},
                                                      fields=["name", "token_map", "payload"], as_list=True)}
        for r in rows:
            token_map, payload = extra.get(r.name, (None, None))
            r["token_map"] = {**json.loads(token_map or "{}"), **_layer_display_map(payload)}
    counts = {s: len(frappe.get_list("AI Action", filters={"status": s}, pluck="name", limit_page_length=0))
              for s in statuses}
    return {"rows": rows, "counts": counts, "can_decide": can_decide, "security_pending": _security_pending(can_decide)}


def _layer_display_map(payload: str | None) -> dict:
    """For HR deciders only: real values behind input-layer tokens, from the inspection report of the
    source email (REPORT key, the dashboard's key) and the Inbound Email's restricted sender field."""
    try:
        p = json.loads(payload or "{}")
        if not (p.get("doc_id") and p.get("email_id")):
            return {}
        inbound = frappe.db.get_value("Inbound Email", p["email_id"], ["report_id", "sender_email", "doc_id"], as_dict=True)
        if not inbound or inbound.doc_id != p["doc_id"]:
            return {}
        from hr_ai.ingest import masked_values
        return {**masked_values(inbound.report_id), "<SENDER>": inbound.sender_email}
    except Exception:
        return {}  # display convenience only: the page still works with placeholders


def _security_pending(can_decide: bool) -> int | None:
    """Quarantined documents waiting for an HR decision (None if the layer is unreachable)."""
    if not can_decide:
        return None
    try:
        from hr_ai.ingest import _call
        return sum(1 for r in _call("GET", "/v1/reports", "hr_ai_ingest_report_key", timeout=10)
                   if r.get("verdict") == "quarantined" and not r.get("decision"))
    except Exception:
        return None
