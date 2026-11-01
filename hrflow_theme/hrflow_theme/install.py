"""Point the site's branding settings at the HRflow logo, and undo it on uninstall.

Only touches four single-value settings. The values that were there before are kept
in a site default so `bench uninstall-app hrflow_theme` puts them back."""
import json

import frappe

LOGO = "/assets/hrflow_theme/images/hrflow-logo.svg"
BRANDING = {
    ("Website Settings", "app_logo"): LOGO,
    ("Website Settings", "app_name"): "HRflow",
    ("Navbar Settings", "app_logo"): LOGO,
}
BACKUP_KEY = "hrflow_theme_previous_branding"


def _key(doctype, field):
    return f"{doctype}|{field}"


def apply_branding():
    previous = {}
    for (doctype, field), value in BRANDING.items():
        previous[_key(doctype, field)] = frappe.db.get_single_value(doctype, field)
        frappe.db.set_single_value(doctype, field, value)
    if not frappe.db.get_default(BACKUP_KEY):
        frappe.db.set_default(BACKUP_KEY, json.dumps(previous))
    frappe.clear_cache()


def restore_branding():
    saved = frappe.db.get_default(BACKUP_KEY)
    previous = json.loads(saved) if saved else {}
    for (doctype, field), value in BRANDING.items():
        # Only restore what HRflow still owns: if someone changed it since, leave it.
        if frappe.db.get_single_value(doctype, field) == value:
            frappe.db.set_single_value(doctype, field, previous.get(_key(doctype, field)) or None)
    frappe.defaults.clear_default(key=BACKUP_KEY, parent="__default")
    frappe.clear_cache()
