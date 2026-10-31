"""candidate_ref / sender_ref: link documents to a candidate without the email.

ref = "hmac:" + HMAC-SHA256(INGEST_REF_KEY, normalized email)

The same email always gives the same ref, so Frappe can match a CV and a
later email from the same person. Without the key, the ref cannot be turned
back into the email (and cannot be recomputed from a guessed email either).
"""

import hashlib
import hmac


def normalize_email(email: str) -> str:
    return email.strip().lower()


def email_ref(email: str, key: bytes) -> str:
    digest = hmac.new(key, normalize_email(email).encode("utf-8"), hashlib.sha256).hexdigest()
    return "hmac:" + digest
