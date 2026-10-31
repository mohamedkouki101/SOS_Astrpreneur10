"""Read-only REST client for RH Flow.

Each agent uses its own API user (least-privilege, read-only role). This class exposes GET
requests only; there is deliberately no method that creates, updates or deletes records.
The only outbound write path for agents is `propose_action` (see tools/outbox.py).
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from agents.config import get_settings


class PlatformError(RuntimeError):
    pass


class MissingCredentials(PlatformError):
    pass


class PlatformClient:
    def __init__(self, agent: str, *, timeout: float = 30.0):
        settings = get_settings()
        creds = settings.credentials(agent)
        if not creds:
            raise MissingCredentials(
                f"No RH Flow API credentials for agent '{agent}'. "
                f"Run agents/scripts/setup_agent_users.py or set RHFLOW_{agent.upper()}_API_KEY/_API_SECRET."
            )
        self.agent = agent
        self._http = httpx.Client(
            base_url=settings.rhflow_url,
            headers={"Authorization": f"token {creds[0]}:{creds[1]}", "Accept": "application/json"},
            timeout=timeout,
        )

    # -- the only HTTP verb used --------------------------------------------------------------
    def _get(self, path: str, params: dict[str, Any] | None = None) -> httpx.Response:
        resp = self._http.get(path, params=params)
        if resp.status_code == 403:
            raise PlatformError(f"403 forbidden for agent '{self.agent}' on {path.split('?')[0]}")
        if resp.status_code == 404:
            raise PlatformError(f"404 not found: {path.split('?')[0]}")
        if resp.status_code >= 400:
            raise PlatformError(f"{resp.status_code} on {path.split('?')[0]}")
        return resp

    def get_doc(self, doctype: str, name: str) -> dict:
        return self._get(f"/api/resource/{doctype}/{name}").json()["data"]

    def get_list(
        self,
        doctype: str,
        *,
        filters: list | dict | None = None,
        fields: list[str] | None = None,
        order_by: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        params: dict[str, Any] = {"limit_page_length": limit}
        if filters:
            params["filters"] = json.dumps(filters)
        if fields:
            params["fields"] = json.dumps(fields)
        if order_by:
            params["order_by"] = order_by
        return self._get(f"/api/resource/{doctype}", params).json()["data"]

    def get_file(self, file_url: str) -> bytes:
        if not file_url.startswith(("/private/files/", "/files/")):
            raise PlatformError("Only site file URLs can be fetched")
        return self._get(file_url).content

    def close(self) -> None:
        self._http.close()
