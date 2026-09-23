"""Small authenticated client for local workers -> shared EPIS runtime."""

from __future__ import annotations

import os
from urllib.parse import urlsplit, urlunsplit
import uuid

import requests


def _http_base_from_server_url(value: str) -> str:
    parsed = urlsplit((value or "").strip())
    if parsed.scheme not in {"ws", "wss", "http", "https"} or not parsed.netloc:
        raise ValueError("invalid_server_url")
    scheme = {
        "ws": "http",
        "wss": "https",
        "http": "http",
        "https": "https",
    }[parsed.scheme]
    return urlunsplit((scheme, parsed.netloc, "", "", "")).rstrip("/")


class SharedRuntimeClient:
    def __init__(self, base_url: str | None = None, token: str | None = None):
        raw = (
            base_url
            or os.getenv("EPIS_SHARED_RUNTIME_URL")
            or os.getenv("EPIS_SERVER_URL")
            or ""
        )
        self.base_url = _http_base_from_server_url(raw) if raw else ""
        self.token = (
            token
            or os.getenv("EPIS_INTERNAL_EVENT_TOKEN")
            or ""
        ).strip()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    def is_ready(self) -> bool:
        if not self.base_url or not self.token:
            return False
        try:
            response = requests.get(f"{self.base_url}/health", timeout=4)
            return response.ok and bool(response.json().get("ok"))
        except Exception:
            return False

    def proactive(
        self,
        trigger_type: str,
        context: str,
        priority: str = "medium",
    ) -> dict:
        if not self.base_url:
            return {"ok": False, "error": "shared_runtime_url_missing"}
        if not self.token:
            return {"ok": False, "error": "internal_event_token_missing"}
        try:
            response = requests.post(
                f"{self.base_url}/internal/proactive",
                headers=self._headers(),
                json={
                    "request_id": f"kairos:{uuid.uuid4().hex}",
                    "trigger_type": trigger_type,
                    "context": context,
                    "priority": priority,
                },
                timeout=45,
            )
        except requests.RequestException as exc:
            return {
                "ok": False,
                "error": "shared_runtime_unreachable",
                "detail": type(exc).__name__,
            }

        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if not response.ok:
            return {
                "ok": False,
                "error": "shared_runtime_rejected",
                "status_code": response.status_code,
                "detail": payload.get("detail") if isinstance(payload, dict) else None,
            }
        return payload if isinstance(payload, dict) else {"ok": False, "error": "invalid_response"}
