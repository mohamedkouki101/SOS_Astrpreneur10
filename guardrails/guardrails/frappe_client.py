"""Minimal Frappe REST client (stdlib only).

Only the Execution Gateway creates a client with WRITE credentials.
Auth header: `Authorization: token KEY:SECRET` (Frappe API key of a
dedicated "guardrails gateway" user with least-privilege roles).
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any, Mapping, Protocol
from urllib import error, parse, request


class FrappeError(Exception):
    """Frappe answered and refused (4xx). The write did NOT happen."""

    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class FrappeUnavailable(Exception):
    """Network error, timeout or 5xx. We do NOT know if the write happened."""


class FrappeAPI(Protocol):
    def get_doc(self, doctype: str, name: str) -> dict: ...
    def list_docs(self, doctype: str, filters: list, fields: list[str], limit: int = 20) -> list[dict]: ...
    def update_doc(self, doctype: str, name: str, fields: Mapping[str, Any]) -> dict: ...
    def insert_doc(self, doctype: str, fields: Mapping[str, Any]) -> dict: ...
    def call(self, method: str, params: Mapping[str, Any]) -> Any: ...


class FrappeClient:
    def __init__(self, base_url: str, api_key: str, api_secret: str, timeout: float = 15.0):
        if not base_url or not api_key or not api_secret:
            raise ValueError("Frappe URL, API key and secret are required")
        if not base_url.startswith(("http://", "https://")):
            raise ValueError("FRAPPE_URL must start with http:// or https://")
        self.base_url = base_url.rstrip("/")
        self._auth = f"token {api_key}:{api_secret}"
        self.timeout = timeout

    @classmethod
    def from_env(cls) -> "FrappeClient":
        return cls(
            os.environ.get("FRAPPE_URL", ""),
            os.environ.get("FRAPPE_GATEWAY_API_KEY", ""),
            os.environ.get("FRAPPE_GATEWAY_API_SECRET", ""),
        )

    def __repr__(self) -> str:  # never print the secret
        return f"FrappeClient({self.base_url!r})"

    # -- resources --

    def get_doc(self, doctype: str, name: str) -> dict:
        return self._request("GET", self._resource(doctype, name))["data"]

    def list_docs(self, doctype: str, filters: list, fields: list[str], limit: int = 20) -> list[dict]:
        query = parse.urlencode({"filters": json.dumps(filters), "fields": json.dumps(fields),
                                 "limit_page_length": str(limit)})
        return self._request("GET", f"{self._resource(doctype)}?{query}")["data"]

    def update_doc(self, doctype: str, name: str, fields: Mapping[str, Any]) -> dict:
        return self._request("PUT", self._resource(doctype, name), fields)["data"]

    def insert_doc(self, doctype: str, fields: Mapping[str, Any]) -> dict:
        return self._request("POST", self._resource(doctype), fields)["data"]

    def call(self, method: str, params: Mapping[str, Any]) -> Any:
        if not all(part.isidentifier() for part in method.split(".")):
            raise ValueError(f"invalid method name: {method!r}")
        return self._request("POST", f"/api/method/{method}", params).get("message")

    # -- internals --

    @staticmethod
    def _resource(doctype: str, name: str | None = None) -> str:
        path = "/api/resource/" + parse.quote(doctype, safe="")
        if name is not None:
            path += "/" + parse.quote(name, safe="")
        return path

    def _request(self, method: str, path: str, body: Mapping[str, Any] | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = request.Request(self.base_url + path, data=data, method=method, headers={
            "Authorization": self._auth,
            "Accept": "application/json",
            "Content-Type": "application/json",
        })
        try:
            with request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except error.HTTPError as e:
            msg = _error_message(e)
            if 400 <= e.code < 500:
                raise FrappeError(e.code, msg) from None
            raise FrappeUnavailable(f"HTTP {e.code}: {msg}") from None
        except (error.URLError, socket.timeout, TimeoutError, ConnectionError) as e:
            raise FrappeUnavailable(str(e)) from None
        try:
            return json.loads(raw) if raw else {}
        except ValueError:
            raise FrappeUnavailable("invalid JSON from Frappe") from None


def _error_message(e: error.HTTPError) -> str:
    try:
        payload = json.loads(e.read() or b"{}")
        return str(payload.get("exception") or payload.get("exc_type") or e.reason)[:300]
    except Exception:
        return str(e.reason)[:300]
