"""Sign-in step in front of each tile of the apps screen (/desk).

Clicking a tile opens a sign-in dialog (public/js/app_gate.js). The credentials are checked HERE,
by Frappe's own LoginManager (same password check, failed-attempt tracking and lockout as
/login), plus a rate limit. The account must fit the tile:

    rh_chat                 a chat user: Website User with the chat role (never a Desk account)
    hrms, hr_ai, frappe     an admin: System Manager

On success the browser's session is REPLACED by a new session for that account (the previous
session is deleted), then the page opens the app. A refused account gets no session.

This is a step-up sign-in on the apps screen. Each app is still protected by its own
permissions when opened by URL directly (/chat needs the chat role, /desk/rh-ai needs an HR role).
"""

import frappe
from frappe import _
from frappe.rate_limiter import rate_limit

from hr_ai.setup import CHAT_ROLE

ADMIN_ROLE = "System Manager"
GATES = {
    "rh_chat": "user",
    "hrms": "admin",
    "hr_ai": "admin",
    "frappe": "admin",
}


def _fits(kind: str, user: str) -> bool:
    roles = set(frappe.get_roles(user))
    if kind == "admin":
        return ADMIN_ROLE in roles
    return (user != "Administrator" and CHAT_ROLE in roles
            and frappe.db.get_value("User", user, "user_type") == "Website User")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60)
def app_login(app: str, usr: str, pwd: str) -> dict:
    kind = GATES.get(app)
    if not kind:
        frappe.throw(_("Unknown app"), frappe.PermissionError)

    from frappe.twofactor import should_run_2fa

    login_manager = frappe.local.login_manager
    login_manager.authenticate(user=usr, pwd=pwd)   # raises on a wrong id/password (and counts it)
    user = login_manager.user
    if should_run_2fa(user):
        frappe.throw(_("This account uses two-factor authentication: sign in from the login page."),
                     frappe.AuthenticationError)
    if not _fits(kind, user):
        frappe.throw(_("This account cannot open this app. Use a chat user account.") if kind == "user"
                     else _("This app needs an administrator account."), frappe.PermissionError)

    old_sid = frappe.session.sid if frappe.session.user != "Guest" else None
    login_manager.post_login()                      # new session + cookies for `user`
    if old_sid and old_sid != frappe.session.sid:
        from frappe.sessions import delete_session
        delete_session(old_sid, reason="Signed in to an app from the apps screen")
    return {"user": user, "kind": kind}
