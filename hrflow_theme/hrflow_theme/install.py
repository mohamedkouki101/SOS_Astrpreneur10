"""Point the site's branding at the HR Flow logo and name, and undo it on uninstall.

Settings: the values that were there before are kept in a site default so
`bench uninstall-app hrflow_theme` puts them back. Safe to run again
(`bench --site <site> execute hrflow_theme.install.apply_branding`): only values not saved yet
are added to the backup.

Translations (English overrides of fixed UI strings such as "Frappe HR") are upserted and are
not removed on uninstall: they are content, like any other Translation record."""
import json

import frappe

APP_NAME = "HR Flow"
LOGO = "/assets/hrflow_theme/images/hrflow-logo.svg"
# Square mark for the browser tab (the "HR" end of the logo). Lives in hr_ai with the other pictograms.
FAVICON = "/assets/hr_ai/images/hr-flow-favicon.svg"

BRANDING = {
    ("Website Settings", "app_logo"): LOGO,
    ("Website Settings", "app_name"): APP_NAME,
    ("Website Settings", "favicon"): FAVICON,
    ("Website Settings", "splash_image"): LOGO,
    ("Website Settings", "brand_html"): f'<img src="{LOGO}" alt="{APP_NAME}" style="height:28px">',
    ("Website Settings", "footer_powered"): APP_NAME,
    ("Navbar Settings", "app_logo"): LOGO,
    ("System Settings", "app_name"): APP_NAME,
}
TRANSLATIONS = {
    "Frappe HR": APP_NAME,
    "Frappe": APP_NAME,
    "ERPNext": f"{APP_NAME} ERP",
    "Framework": f"{APP_NAME} Admin",
    "Welcome! Please sign in to continue.": f"Welcome to {APP_NAME}! Please sign in to continue.",
}
LOGIN_TITLE_SCRIPT = f'if (location.pathname === "/login") {{ document.title = "Login | {APP_NAME}"; }}'
BACKUP_KEY = "hrflow_theme_previous_branding"


def _key(doctype, field):
    return f"{doctype}|{field}"


def apply_branding():
    saved = frappe.db.get_default(BACKUP_KEY)
    previous = json.loads(saved) if saved else {}
    for (doctype, field), value in BRANDING.items():
        current = frappe.db.get_single_value(doctype, field)
        previous.setdefault(_key(doctype, field), current)
        if current != value:
            frappe.db.set_single_value(doctype, field, value)
    frappe.db.set_default(BACKUP_KEY, json.dumps(previous))

    for source, target in TRANSLATIONS.items():
        name = frappe.db.get_value("Translation", {"language": "en", "source_text": source})
        if name:
            doc = frappe.get_doc("Translation", name)
            if doc.translated_text != target:
                doc.translated_text = target
                doc.save(ignore_permissions=True)   # save (not db.set_value) so the translation cache is cleared
        else:
            frappe.get_doc({"doctype": "Translation", "language": "en", "source_text": source,
                            "translated_text": target}).insert(ignore_permissions=True)

    if frappe.db.exists("DocType", "Website Script"):
        frappe.db.set_single_value("Website Script", "javascript", LOGIN_TITLE_SCRIPT)
    frappe.db.commit()
    from frappe.core.doctype.translation.translation import clear_user_translation_cache
    from frappe.website.utils import clear_cache as clear_website_cache

    clear_user_translation_cache("en")
    clear_website_cache()   # login page and other website pages render the favicon from cache
    frappe.clear_cache()


def restore_branding():
    saved = frappe.db.get_default(BACKUP_KEY)
    previous = json.loads(saved) if saved else {}
    for (doctype, field), value in BRANDING.items():
        # Only restore what HR Flow still owns: if someone changed it since, leave it.
        if frappe.db.get_single_value(doctype, field) == value:
            frappe.db.set_single_value(doctype, field, previous.get(_key(doctype, field)) or None)
    frappe.defaults.clear_default(key=BACKUP_KEY, parent="__default")
    frappe.clear_cache()
