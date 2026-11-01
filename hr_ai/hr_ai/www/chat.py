"""/chat: RH Chat page for employees and job applicants (website users, no Desk access)."""

import frappe

from hr_ai.chat import CHAT_ROLES

no_cache = 1


def get_context(context):
    if frappe.session.user == "Guest":
        frappe.local.flags.redirect_location = "/login?redirect-to=/chat"
        raise frappe.Redirect
    if not CHAT_ROLES & set(frappe.get_roles()):
        raise frappe.PermissionError("RH Chat is not available for this account")
    context.no_header = 1
    context.no_breadcrumbs = 1
    context.full_width = 1
    context.title = "RH Chat"
    context.first_name = frappe.db.get_value("User", frappe.session.user, "first_name") or frappe.session.user
    context.csrf_token = frappe.sessions.get_csrf_token()
