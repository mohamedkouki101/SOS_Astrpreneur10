"""Chat Message: one record per message in HR Chat (user -> assistant, assistant -> user).

Created only by hr_ai.chat (never from Desk or REST). A chat user can read only their own messages.
The user's text is sent to the input security layer BEFORE any AI reads it; the agents read the
masked, signed envelope, never this record. `masked_content` (HR only) is what the agents produced.
"""

import frappe
from frappe.model.document import Document


class ChatMessage(Document):
    def validate(self):
        if not self.flags.from_chat:
            frappe.throw("Chat messages are written by HR Chat only")
