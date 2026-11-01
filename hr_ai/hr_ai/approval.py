"""Human approval: canonical action hash and an approval token bound to it.

The approval token is an HMAC over (AI Action name, action_hash, approver) with a site secret.
Any change to the payload changes the action_hash, which voids the token (see ai_action.py).
Key management is an open question (design notes); for now the secret lives in site_config.json.
"""

import hashlib
import hmac
import json

import frappe

KEY_NAME = "hr_ai_approval_secret"


def canonical_hash(kind: str, subject: str, payload, source_hashes) -> str:
    """Must match agents/validation/validator.py:action_hash exactly."""
    if isinstance(payload, str):
        payload = json.loads(payload or "{}")
    if isinstance(source_hashes, str):
        source_hashes = json.loads(source_hashes or "[]")
    canonical = json.dumps(
        {"kind": kind, "subject": subject, "payload": payload, "source_hashes": sorted(source_hashes)},
        sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str,
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def ensure_signing_key():
    if not frappe.conf.get(KEY_NAME):
        from frappe.installer import update_site_config

        update_site_config(KEY_NAME, frappe.generate_hash(length=64))


def _secret() -> bytes:
    key = frappe.conf.get(KEY_NAME)
    if not key:
        frappe.throw("Approval signing key is not configured")
    return key.encode()


def sign(name: str, action_hash: str, approver: str) -> str:
    return hmac.new(_secret(), f"{name}|{action_hash}|{approver}".encode(), hashlib.sha256).hexdigest()


def verify(name: str, action_hash: str, approver: str, token: str) -> bool:
    return bool(token) and hmac.compare_digest(sign(name, action_hash, approver), token)
