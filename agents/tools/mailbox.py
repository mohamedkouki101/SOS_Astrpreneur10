"""HR inbox access (read-only). Two backends, chosen by MAIL_SOURCE:

- "mailpit": the HR inbox is every message addressed to HR_INBOX_ADDRESS in the local Mailpit test
  mail server (MAILPIT_URL). Raw .eml bytes are fetched and then go through ingest stages 1-4 like any
  other untrusted input. Nothing is ever sent, deleted or marked from here.
- "folder" (default, used by tests): the fictional .eml files in the dataset.
"""

from __future__ import annotations

import re

import httpx

from agents.config import get_settings
from agents.tools.common import dataset

_ID = re.compile(r"^[\w.-]{1,64}$")


def check_id(email_id: str) -> str:
    email_id = email_id.strip().removesuffix(".eml")
    if not _ID.fullmatch(email_id):
        raise ValueError("invalid email id")
    return email_id


def list_ids(limit: int = 30) -> list[str]:
    """Email ids in the HR inbox, oldest first."""
    s = get_settings()
    limit = max(1, min(limit, 100))
    if s.mail_source == "mailpit":
        r = httpx.get(f"{s.mailpit_url}/api/v1/search", params={"query": f"to:{s.hr_inbox_address}", "limit": limit},
                      timeout=15)
        r.raise_for_status()
        return [m["ID"] for m in reversed(r.json().get("messages", []))]
    return [p.stem for p in sorted(dataset("emails").glob("*.eml"))][:limit]


def raw(email_id: str) -> bytes:
    """Raw .eml bytes of one inbox message (untrusted: callers must run the ingest pipeline)."""
    s = get_settings()
    email_id = check_id(email_id)
    if s.mail_source == "mailpit":
        r = httpx.get(f"{s.mailpit_url}/api/v1/message/{email_id}/raw", timeout=15)
        if r.status_code == 404:
            raise FileNotFoundError(f"email {email_id} not found")
        r.raise_for_status()
        return r.content
    path = dataset("emails", f"{email_id}.eml")
    if not path.is_file():
        raise FileNotFoundError(f"email {email_id} not found")
    return path.read_bytes()


def exists(email_id: str) -> bool:
    try:
        raw(email_id)
        return True
    except (FileNotFoundError, ValueError, httpx.HTTPError):
        return False
