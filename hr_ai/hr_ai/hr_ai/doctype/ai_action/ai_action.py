"""AI Action: one record per action proposed by an agent.

Workflow: Proposed -> Approved | Rejected;  Approved -> Executed | Failed (executor, build plan task 3).
- Status can only change through approve()/reject() (or the executor), never by editing the field.
- Editing the payload recomputes action_hash, resets the status to Proposed and clears the approval.
"""

import json

import frappe
from frappe.model.document import Document
from frappe.utils import now_datetime

from hr_ai.approval import canonical_hash, sign

APPROVER_ROLES = {"HR Manager", "System Manager"}


class AIAction(Document):
    def validate(self):
        try:
            json.loads(self.payload or "{}")
        except ValueError:
            frappe.throw("Payload must be valid JSON")
        new_hash = canonical_hash(self.kind, self.subject, self.payload, self.source_hashes)

        if self.is_new():
            if not self.flags.from_propose_action:
                frappe.throw("AI Actions can only be created by agents through hr_ai.api.propose_action")
            self.status = "Proposed"
            self.action_hash = new_hash
            return

        if self.has_value_changed("payload") and new_hash != self.action_hash:
            self.action_hash = new_hash
            self.status = "Proposed"
            self.approver = self.approved_on = self.approval_token = None
            self.flags.status_change_allowed = True
            self.add_comment("Info", "Payload edited by a human: approval voided, action back to Proposed.")

        if self.has_value_changed("status") and not self.flags.status_change_allowed:
            frappe.throw("Status can only change through Approve / Reject (or the executor)")

    def _decide(self, status: str, note: str | None = None):
        if not APPROVER_ROLES & set(frappe.get_roles()):
            frappe.throw("Only HR Managers can approve or reject AI Actions", frappe.PermissionError)
        if self.status != "Proposed":
            frappe.throw(f"Only Proposed actions can be decided (current status: {self.status})")
        if canonical_hash(self.kind, self.subject, self.payload, self.source_hashes) != self.action_hash:
            frappe.throw("Integrity check failed: payload does not match action_hash")
        self.status = status
        self.approver = frappe.session.user
        self.approved_on = now_datetime()
        self.decision_note = note or self.decision_note
        self.approval_token = sign(self.name, self.action_hash, self.approver) if status == "Approved" else None
        self.flags.status_change_allowed = True
        self.save()

    @frappe.whitelist()
    def approve(self, note: str | None = None):
        self._decide("Approved", note)
        return self.status

    @frappe.whitelist()
    def reject(self, note: str | None = None):
        self._decide("Rejected", note)
        return self.status
