"""Install / migrate hooks: approval key, the fields linking RH Flow records to the input security layer,
and the chat (RH Chat) role and identity links.

Demo users (run once, passwords passed on the command line, never stored in git):
    bench --site frontend execute hr_ai.setup.setup_demo_users \
        --kwargs "{'chat_password': '...', 'admin_password': '...'}"
"""

import frappe
from frappe.permissions import add_permission
from frappe.sessions import clear_sessions
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils.password import update_password

from hr_ai.approval import ensure_signing_key

VERDICTS = "\nclean\nsuspicious\nquarantined\nreleased\nrejected\nerror"
CHAT_ROLE = "RH Flow Chat User"
CHAT_USER = "utilisateur@rhflow.local"
CHAT_USERNAME = "Utilisateur"

# Guardrails service (guardrails/, port 8100). Two accounts, neither held by an agent:
#  - CHECK: hr_ai authenticates to POST /agent-actions/check with it (the endpoint wants an HR
#    approver identity). Its key lives in site config only.
#  - READER: the guardrails look people up in HR Core with it (recipient = a known record, token
#    values = that record's values). Read-only role: the Execution Gateway stays the only writer.
GUARDRAILS_READER_ROLE = "RH Flow Guardrails Reader"
# The guardrails learn the caller's roles by reading the caller's OWN User record; its roles table
# is permlevel 1. This role may read permlevel 1 of User records it can open, i.e. only its own.
GUARDRAILS_CALLER_ROLE = "RH Flow Guardrails Caller"
# (doctype, permlevel): people records, and the inbox mail sender (an HR-only permlevel-1 field).
GUARDRAILS_READS = (("Job Applicant", 0), ("Employee", 0), ("Inbound Email", 0), ("Inbound Email", 1))
GUARDRAILS_CHECK_USER = "guardrails.check@rhflow.local"
GUARDRAILS_READER_USER = "guardrails.reader@rhflow.local"

CUSTOM_FIELDS = {
    "Job Applicant": [
        {"fieldname": "rhflow_ingest_section", "fieldtype": "Section Break", "label": "Input security layer",
         "insert_after": "resume_link", "collapsible": 1},
        {"fieldname": "rhflow_ingest_verdict", "fieldtype": "Select", "label": "CV security verdict",
         "options": VERDICTS, "read_only": 1, "in_standard_filter": 1, "insert_after": "rhflow_ingest_section"},
        {"fieldname": "rhflow_ingest_report_id", "fieldtype": "Data", "label": "Inspection report",
         "read_only": 1, "insert_after": "rhflow_ingest_verdict"},
        {"fieldname": "rhflow_ingest_doc_id", "fieldtype": "Data", "label": "CV hash (SHA-256)",
         "read_only": 1, "insert_after": "rhflow_ingest_report_id"},
    ],
    # Who a chat user is in HR Core. Set by HR (System Manager), never by the chat or the AI.
    # Kept on the User (not Employee.user_id) so the chat user never gains the Employee desk role.
    "User": [
        {"fieldname": "rhflow_chat_section", "fieldtype": "Section Break", "label": "RH Chat identity",
         "insert_after": "user_type", "collapsible": 1},
        {"fieldname": "rhflow_chat_employee", "fieldtype": "Link", "options": "Employee",
         "label": "Chat: linked employee", "insert_after": "rhflow_chat_section"},
        {"fieldname": "rhflow_chat_applicant", "fieldtype": "Link", "options": "Job Applicant",
         "label": "Chat: linked job applicant", "insert_after": "rhflow_chat_employee"},
    ],
}


def after_install():
    ensure_signing_key()
    after_migrate()


def after_migrate():
    create_custom_fields(CUSTOM_FIELDS, update=True)
    ensure_chat_role()
    ensure_guardrails_roles()


def ensure_guardrails_roles():
    if not frappe.db.exists("Role", GUARDRAILS_READER_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": GUARDRAILS_READER_ROLE, "desk_access": 1}
                       ).insert(ignore_permissions=True)
    for doctype, level in GUARDRAILS_READS:
        if not frappe.db.exists("Custom DocPerm", {"parent": doctype, "role": GUARDRAILS_READER_ROLE,
                                                   "permlevel": level}):
            add_permission(doctype, GUARDRAILS_READER_ROLE, level)   # read only
    if not frappe.db.exists("Role", GUARDRAILS_CALLER_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": GUARDRAILS_CALLER_ROLE, "desk_access": 1}
                       ).insert(ignore_permissions=True)
    if not frappe.db.exists("Custom DocPerm", {"parent": "User", "role": GUARDRAILS_CALLER_ROLE, "permlevel": 1}):
        add_permission("User", GUARDRAILS_CALLER_ROLE, 1)   # read only


def _service_user(email: str, first_name: str, roles: list[str]) -> dict:
    """Create (or reset) a service user with exactly these roles and a fresh API key."""
    if frappe.db.exists("User", email):
        user = frappe.get_doc("User", email)
    else:
        user = frappe.get_doc({"doctype": "User", "email": email, "first_name": first_name,
                               "user_type": "System User", "send_welcome_email": 0})
    user.set("roles", [{"role": r} for r in roles])
    user.api_key = user.api_key or frappe.generate_hash(length=15)
    secret = frappe.generate_hash(length=15)
    user.api_secret = secret
    user.flags.ignore_permissions = True
    user.save() if not user.is_new() else user.insert()
    return {"api_key": user.api_key, "api_secret": secret}


def setup_guardrails_accounts(url: str = "http://guardrails:8100") -> dict:
    """Run once (keys are regenerated on every run):
        bench --site frontend execute hr_ai.setup.setup_guardrails_accounts
    Stores the CHECK key in site config and returns the READER key for guardrails/.env."""
    from frappe.installer import update_site_config

    ensure_guardrails_roles()
    check = _service_user(GUARDRAILS_CHECK_USER, "Guardrails Check", ["HR Manager", GUARDRAILS_CALLER_ROLE])
    reader = _service_user(GUARDRAILS_READER_USER, "Guardrails Reader", [GUARDRAILS_READER_ROLE])
    update_site_config("hr_ai_guardrails_url", url)
    update_site_config("hr_ai_guardrails_api_key", check["api_key"])
    update_site_config("hr_ai_guardrails_api_secret", check["api_secret"])
    frappe.db.commit()
    return {"reader_api_key": reader["api_key"], "reader_api_secret": reader["api_secret"]}


def ensure_chat_role():
    """The chat role never opens Desk: its users stay Website Users (enforced on every migrate)."""
    if not frappe.db.exists("Role", CHAT_ROLE):
        frappe.get_doc({"doctype": "Role", "role_name": CHAT_ROLE, "desk_access": 0}).insert(ignore_permissions=True)
    if frappe.db.get_value("Role", CHAT_ROLE, "desk_access"):
        frappe.db.set_value("Role", CHAT_ROLE, "desk_access", 0)
    for user in frappe.get_all("Has Role", filters={"role": CHAT_ROLE, "parenttype": "User"}, pluck="parent"):
        if user != "Administrator" and frappe.db.get_value("User", user, "user_type") != "Website User":
            frappe.db.set_value("User", user, "user_type", "Website User")
            frappe.cache.delete_key("user_type")
            clear_sessions(user, force=True)


def setup_demo_users(chat_password: str, admin_password: str | None = None, employee: str = "HR-EMP-00007",
                     job_opening: str | None = None) -> dict:
    """Create the chat demo user (login "Utilisateur") and link it to one fictional employee and one
    fictional job applicant. The chat agent decides from the conversation which of the two applies."""
    ensure_chat_role()
    if admin_password:
        update_password("Administrator", admin_password)
    if not frappe.db.exists("User", CHAT_USER):
        frappe.get_doc({
            "doctype": "User", "email": CHAT_USER, "first_name": CHAT_USERNAME, "username": CHAT_USERNAME,
            "user_type": "Website User", "send_welcome_email": 0, "roles": [{"role": CHAT_ROLE}],
        }).insert(ignore_permissions=True)
    update_password(CHAT_USER, chat_password)
    frappe.db.set_single_value("System Settings", "allow_login_using_user_name", 1)

    applicant = frappe.db.get_value("Job Applicant", {"email_id": CHAT_USER}, "name")
    if not applicant:
        job_opening = job_opening or frappe.db.get_value("Job Opening", {"status": "Open"}, "name")
        doc = frappe.get_doc({"doctype": "Job Applicant", "applicant_name": "Utilisateur Demo",
                              "email_id": CHAT_USER, "job_title": job_opening, "status": "Open"})
        doc.insert(ignore_permissions=True)
        applicant = doc.name
    if employee and not frappe.db.exists("Employee", employee):
        frappe.throw(f"Employee {employee} not found")
    frappe.db.set_value("User", CHAT_USER, {"rhflow_chat_employee": employee or None,
                                           "rhflow_chat_applicant": applicant})
    frappe.db.commit()
    return {"user": CHAT_USER, "login": CHAT_USERNAME, "employee": employee, "applicant": applicant}
