"""Install / migrate hooks: approval key and the fields linking RH Flow records to the input security layer."""

from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from hr_ai.approval import ensure_signing_key

VERDICTS = "\nclean\nsuspicious\nquarantined\nreleased\nrejected\nerror"

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
    ]
}


def after_install():
    ensure_signing_key()
    after_migrate()


def after_migrate():
    create_custom_fields(CUSTOM_FIELDS, update=True)
