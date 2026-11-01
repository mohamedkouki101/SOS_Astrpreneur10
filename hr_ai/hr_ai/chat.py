"""RH Chat: employees and job applicants talk to the RH Flow assistant in plain language.

    user types -> send() stores the message (status Checking) and queues process()
    process(): 1. INPUT SECURITY LAYER first (POST /v1/email, UPLOAD key): sanitize, mask PII, scan
               2. held back (quarantined / rejected)? -> "Blocked" reply, HR sees it in the Security tab;
                  the AI never reads it
               3. clean -> signed call to the agent service (/chat) with ONLY the report id and the
                  identities linked to this account by HR; the agents fetch the masked envelope
               4. the reply is stored for the user; tokens from the user's OWN messages are shown back
                  with their real values (the agents never see them)
Anything that would change data becomes an AI Action that HR approves in RH AI; the chat never acts.
"""

import re

import frappe
from frappe.utils import add_to_date, now_datetime

from hr_ai.ingest import SENDER, IngestError, _call, masked_values, verdict_of
from hr_ai.setup import CHAT_ROLE
from hr_ai.webhooks import AgentsError, call_agents

MAX_LEN = 2000
MAX_PER_HOUR = 30
HISTORY_TURNS = 3
CHAT_ROLES = {CHAT_ROLE, "System Manager"}
PENDING = ("Checking", "Thinking")
LAYER_TOKEN = re.compile(r"<[A-Z_]+_[1-9][0-9]{0,3}>")
FIELDS = ["name", "direction", "status", "persona", "content", "ai_action", "verdict", "creation", "in_reply_to"]

BLOCKED_REPLY = ("Your message was held back by the security check, so the assistant did not read it. "
                 "HR has been notified and will review it. If this is a mistake, please rephrase your "
                 "message without instructions aimed at the assistant.")
LAYER_DOWN_REPLY = "The security check is unavailable right now, so your message was not processed. Please try again later."
LAYER_REFUSED_REPLY = "The security check could not accept this message, so it was not processed. Please rephrase it."
AGENTS_DOWN_REPLY = "The assistant is unavailable right now. Your message was checked and kept; please try again later."


def _require_chat_user() -> str:
    if frappe.session.user == "Guest" or not CHAT_ROLES & set(frappe.get_roles()):
        frappe.throw("RH Chat is not available for this account", frappe.PermissionError)
    return frappe.session.user


@frappe.whitelist(methods=["POST"])
def send(message: str) -> dict:
    user = _require_chat_user()
    message = (message or "").strip()
    if not message:
        frappe.throw("Empty message")
    if len(message) > MAX_LEN:
        frappe.throw(f"Message too long (max {MAX_LEN} characters)")
    if frappe.db.exists("Chat Message", {"user": user, "direction": "In", "status": ["in", PENDING]}):
        frappe.throw("Please wait for the answer to your previous message")
    recent = frappe.db.count("Chat Message", {"user": user, "direction": "In",
                                              "creation": [">", add_to_date(now_datetime(), hours=-1)]})
    if recent >= MAX_PER_HOUR:
        frappe.throw("Too many messages in the last hour, please try again later")
    doc = _insert(user=user, direction="In", status="Checking", content=message)
    frappe.enqueue("hr_ai.chat.process", queue="long", timeout=900, name=doc.name, enqueue_after_commit=True)
    return {"name": doc.name}


@frappe.whitelist(methods=["GET"])
def history(limit: int = 60) -> list[dict]:
    user = _require_chat_user()
    rows = frappe.get_all("Chat Message", filters={"user": user}, fields=FIELDS, order_by="creation desc",
                          limit=min(int(limit), 200))
    return list(reversed(rows))


@frappe.whitelist(methods=["POST"])
def clear() -> dict:
    """Start a new conversation: older messages stay stored, the agents just stop using them as context."""
    user = _require_chat_user()
    since = str(now_datetime())
    frappe.cache.set_value(f"hr_ai_chat_since:{user}", since)
    return {"since": since}


def _insert(**fields):
    doc = frappe.get_doc({"doctype": "Chat Message", **fields})
    doc.flags.from_chat = True
    doc.insert(ignore_permissions=True)
    return doc


def _update(name: str, **fields):
    frappe.db.set_value("Chat Message", name, fields)


def _reply(msg, status: str, content: str, **extra):
    _update(msg.name, status=status, persona=extra.get("persona"))
    _insert(user=msg.user, direction="Out", status=status, content=content, in_reply_to=msg.name, **extra)
    frappe.db.commit()


def process(name: str):
    """Background job. Idempotent: a message is processed once (status leaves Checking first)."""
    msg = frappe.get_doc("Chat Message", name, for_update=True)
    if msg.direction != "In" or msg.status != "Checking":
        return msg.status

    # 1. Security layer first. The chat text is treated exactly like an inbound email.
    try:
        summary = _call("POST", "/v1/email", "hr_ai_ingest_upload_key", json={
            "body": msg.content, "is_html": False, "sender_email": _sender(msg.user),
            "message_id": f"<{msg.name}@chat.rhflow.local>"})
    except IngestError as exc:
        frappe.log_error(title=f"RH Chat {name}: input security layer call failed")
        return _reply(msg, "Error", LAYER_REFUSED_REPLY if exc.status and exc.status < 500 else LAYER_DOWN_REPLY)
    verdict = verdict_of(summary)
    _update(name, report_id=summary["report_id"], doc_id=summary["doc_id"], verdict=verdict)

    # 2. Held back: the AI never sees it.
    if not summary.get("released_to_ai"):
        return _reply(msg, "Blocked", BLOCKED_REPLY, verdict=verdict)

    # 3. Clean: the agents get ids only, and the identities HR linked to this account.
    _update(name, status="Thinking")
    frappe.db.commit()
    links = frappe.db.get_value("User", msg.user, ["rhflow_chat_employee", "rhflow_chat_applicant"], as_dict=True) or {}
    turns = _history_for_agents(msg)
    event = {"event_id": f"chat:{name}", "type": "chat.message", "message": name,
             "report_id": summary["report_id"], "history": turns,
             "identities": {"employee": links.get("rhflow_chat_employee"),
                            "applicant": links.get("rhflow_chat_applicant")}}
    try:
        result = call_agents("/chat", event, timeout=600)
    except AgentsError:
        frappe.log_error(title=f"RH Chat {name}: agent service unavailable")
        return _reply(msg, "Error", AGENTS_DOWN_REPLY)
    # New transaction: the AI Action the agent just proposed was committed by another request, and
    # this job's REPEATABLE READ snapshot (taken before the call) would not see it.
    frappe.db.commit()

    # 4. Reply. The user's own masked values are shown back to the same user; nothing else is unmasked.
    masked = str(result.get("reply") or "").strip()[:4000] or "Sorry, I could not find an answer."
    report_ids = [summary["report_id"]] + [t["report_id"] for t in turns if t.get("report_id")]
    persona = result.get("persona") if result.get("persona") in ("employee", "applicant", "unknown") else None
    proposals = [p for p in result.get("proposals") or [] if isinstance(p, str)]
    ai_action = proposals[0] if proposals and frappe.db.exists("AI Action", proposals[0]) else None
    _reply(msg, "Answered", _unmask(masked, report_ids), persona=persona, ai_action=ai_action,
           masked_content=masked, run_id=str(result.get("run_id") or "")[:40], verdict=verdict)
    return "Answered"


def _sender(user: str) -> str:
    """The layer needs an email-shaped sender. Accounts like "Administrator" have a name, not an email."""
    email = user if SENDER.fullmatch(user or "") else frappe.db.get_value("User", user, "email")
    return email if SENDER.fullmatch(email or "") else "chat-user@chat.rhflow.local"


def _history_for_agents(msg) -> list[dict]:
    """Previous turns of this conversation: the user's turns as report ids (the agents fetch the
    masked envelope), the assistant's turns as the masked text it produced itself."""
    since = frappe.cache.get_value(f"hr_ai_chat_since:{msg.user}")
    filters = {"user": msg.user, "creation": ["<", msg.creation]}
    if since:
        filters["creation"] = ["between", [since, msg.creation]]
    rows = frappe.get_all("Chat Message", filters=filters,
                          fields=["direction", "status", "report_id", "masked_content", "in_reply_to"],
                          order_by="creation desc", limit=HISTORY_TURNS * 2)
    turns = []
    for r in reversed(rows):
        if r.status != "Answered":
            continue  # blocked or failed turns are never shown to the agents
        if r.direction == "In" and r.report_id:
            turns.append({"role": "user", "report_id": r.report_id})
        elif r.direction == "Out" and r.masked_content:
            turns.append({"role": "assistant", "text": r.masked_content[:1500]})
    return turns


def _unmask(text: str, report_ids: list[str]) -> str:
    if not LAYER_TOKEN.search(text):
        return text
    values = {}
    for rid in report_ids:
        try:
            for token, original in masked_values(rid).items():
                values.setdefault(token, original)
        except IngestError:
            continue
    return LAYER_TOKEN.sub(lambda m: values.get(m.group(0), m.group(0)), text)
