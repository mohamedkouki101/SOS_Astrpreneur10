"""Who is calling? Asked to Frappe, never trusted from the request.

The caller sends its own Frappe credentials:
  - `Authorization: token KEY:SECRET` (agents, scripts, HR UI with API key), or
  - the Frappe session cookie `sid` (HR logged into Desk in the same browser).
We forward them to Frappe and ask "who is this user and what roles do they
have?". Frappe is the single source of truth for identity and roles.

Cookie auth is only accepted on state-changing requests if the custom
header `X-Guardrails-CSRF: 1` is present. Browsers cannot add custom
headers cross-origin without a CORS preflight, which our CORS allowlist
refuses, so a malicious page cannot ride the HR session (CSRF).
"""
from __future__ import annotations

import hashlib
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Mapping, Protocol
from urllib import error, request

from .frappe_client import FrappeUnavailable

CSRF_HEADER = "x-guardrails-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass(frozen=True)
class Identity:
    user: str
    roles: frozenset[str]


class AuthError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


class Authenticator(Protocol):
    def authenticate(self, method: str, headers: Mapping[str, str],
                     cookies: Mapping[str, str]) -> Identity: ...


def credential_from(method: str, headers: Mapping[str, str], cookies: Mapping[str, str]) -> dict:
    """Extract the caller's credential as headers to forward to Frappe."""
    auth = headers.get("authorization", "")
    if auth:
        if not auth.startswith("token ") or ":" not in auth[6:]:
            raise AuthError(401, "bad_credentials", "use 'Authorization: token KEY:SECRET'")
        return {"Authorization": auth}
    sid = cookies.get("sid")
    if sid and sid != "Guest":
        if method.upper() not in SAFE_METHODS and headers.get(CSRF_HEADER) != "1":
            raise AuthError(403, "csrf", f"cookie auth requires the {CSRF_HEADER} header")
        return {"Cookie": f"sid={sid}"}
    raise AuthError(401, "unauthenticated", "Frappe API token or session required")


class FrappeAuthenticator:
    """Resolves identity with two read-only Frappe calls, cached briefly."""

    def __init__(self, base_url: str, timeout: float = 5.0, cache_seconds: float = 60.0,
                 max_cache: int = 1000):
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("FRAPPE_URL must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.cache_seconds = cache_seconds
        self.max_cache = max_cache
        self._cache: dict[str, tuple[float, Identity]] = {}
        self._lock = threading.Lock()

    def authenticate(self, method, headers, cookies) -> Identity:
        cred = credential_from(method, headers, cookies)
        key = hashlib.sha256(json.dumps(cred, sort_keys=True).encode()).hexdigest()
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and hit[0] > now:
                return hit[1]

        user = self._get(cred, "frappe.auth.get_logged_user")
        if not isinstance(user, str) or not user or user == "Guest":
            raise AuthError(401, "unauthenticated", "not logged in to Frappe")
        roles = self._get(cred, "frappe.core.doctype.user.user.get_roles")
        if not isinstance(roles, list):
            raise AuthError(503, "frappe_unavailable", "could not read roles from Frappe")
        ident = Identity(user, frozenset(r for r in roles if isinstance(r, str)))

        with self._lock:
            if len(self._cache) >= self.max_cache:
                self._cache.clear()
            self._cache[key] = (now + self.cache_seconds, ident)
        return ident

    def _get(self, cred: dict, method: str):
        req = request.Request(f"{self.base_url}/api/method/{method}", method="GET",
                              headers={**cred, "Accept": "application/json"})
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}").get("message")
        except error.HTTPError as e:
            if e.code in (401, 403):
                raise AuthError(401, "unauthenticated", "Frappe rejected the credentials") from None
            raise AuthError(503, "frappe_unavailable", f"Frappe answered HTTP {e.code}") from None
        except (error.URLError, socket.timeout, TimeoutError, ConnectionError, ValueError, FrappeUnavailable):
            raise AuthError(503, "frappe_unavailable", "Frappe is unreachable") from None
