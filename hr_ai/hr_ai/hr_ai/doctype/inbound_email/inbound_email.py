"""Inbound Email: one record per message received in the HR inbox, AFTER the input security layer.

Created only by hr_ai.ingest.sync_inbox (nobody can create or edit it from Desk or REST).
Level 0 (agents may read): verdict, report/doc ids, linked applicant. Level 1 (HR only): sender, subject.
The message content is never stored here: the AI reads the masked, signed envelope from the layer.
"""

import frappe
from frappe.model.document import Document


class InboundEmail(Document):
    def validate(self):
        if self.is_new() and not self.flags.from_ingest:
            frappe.throw("Inbound Emails are created by the inbox sync only")
