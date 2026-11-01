"""Install / migrate hooks: approval key, the fields linking RH Flow records to the input security layer,
and the chat (RH Chat) role and identity links.

Demo users (run once, passwords passed on the command line, never stored in git):
    bench --site frontend execute hr_ai.setup.setup_demo_users \
        --kwargs "{'chat_password': '...', 'admin_password': '...'}"
"""

import frappe
from frappe.sessions import clear_sessions
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields
from frappe.utils.password import update_password

from hr_ai.approval import ensure_signing_key

VERDICTS = "\nclean\nsuspicious\nquarantined\nreleased\nrejected\nerror"
CHAT_ROLE = "RH Flow Chat User"
CHAT_USER = "utilisateur@rhflow.local"
CHAT_USERNAME = "Utilisateur"

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
