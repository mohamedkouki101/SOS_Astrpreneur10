"""Apps screen (/desk) for RH Flow: the ERPNext tile is replaced by the RH Chat tile.

ERPNext stays installed (HRMS depends on it) and its pages keep working; only its tile leaves the
apps screen. The change is applied to this user's boot data, so nothing in erpnext is modified.
"""

import frappe

HIDDEN_APPS = {"erpnext"}
CHAT_TILE = {
    "on_apps_screen": True,
    "sequence_id": 1,
    "app_name": "rh_chat",
    "app_title": "RH Chat",
    "app_route": "/chat",
    "desk_route": "",
    "app_logo_url": "/assets/hr_ai/images/rh-chat-logo.svg",
    "dock": [],
}


def extend_bootinfo(bootinfo):
    apps = bootinfo.get("app_data") or []
    for app in apps:
        if app.get("app_name") in HIDDEN_APPS:
            app["on_apps_screen"] = False
    if frappe.session.user != "Guest" and not any(a.get("app_name") == "rh_chat" for a in apps):
        apps.append(dict(CHAT_TILE))
    bootinfo["app_data"] = apps


def home_page(user: str) -> str | None:
    """Only chat users who are Website Users go to /chat (Administrator holds every role: not them)."""
    if user in ("Administrator", "Guest") or frappe.db.get_value("User", user, "user_type") != "Website User":
        return None
    return "chat" if "RH Flow Chat User" in frappe.get_roles(user) else None
