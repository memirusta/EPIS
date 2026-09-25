"""Trusted-local client for the shared daily transcript lifecycle.

Nightly Recalculation runs on the trusted local node.  It freezes a cloud day,
processes the immutable payload into the local memory vault, then acknowledges
that exact ``day_id + input_hash`` so the server may purge raw messages.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from typing import Any

import requests
from dotenv import load_dotenv


_THIS_DIR = Path(__file__).resolve().parent
_EPIS_ROOT = _THIS_DIR.parents[1]
load_dotenv(_EPIS_ROOT / "Layer-3" / "keys.env", override=False)


def _server_http_url() -> str:
    candidates = (
        os.getenv("EPIS_SERVER_HTTP_URL"),
        os.getenv("EPIS_SHARED_RUNTIME_URL"),
        os.getenv("EPIS_SERVER_URL"),
        os.getenv("EPIS_RUNTIME_URL"),
    )
    raw = next((str(v).strip() for v in candidates if str(v or "").strip()), "")
    if not raw:
        raise RuntimeError("shared_runtime_url_missing")

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme == "wss":
        scheme = "https"
    elif scheme == "ws":
        scheme = "http"
    elif scheme not in {"http", "https"}:
        raise RuntimeError("shared_runtime_url_invalid")

    path = parts.path.rstrip("/")
    for suffix in ("/device/ws", "/ws"):
        if path.endswith(suffix):
            path = path[: -len(suffix)]
            break
    return urlunsplit((scheme, parts.netloc, path, "", "")).rstrip("/")


class CloudTranscriptClient:
    def __init__(self, *, base_url: str | None = None, token: str | None = None, timeout: float = 30.0):
        self.base_url = (base_url or _server_http_url()).rstrip("/")
        self.token = (token or os.getenv("EPIS_INTERNAL_EVENT_TOKEN") or "").strip()
        if not self.token:
            raise RuntimeError("EPIS_INTERNAL_EVENT_TOKEN_missing")
        self.timeout = float(timeout)

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _json(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        response = requests.request(
            method,
            self.base_url + path,
            headers=self.headers,
            json=body,
            timeout=self.timeout if timeout is None else float(timeout),
        )
        if response.status_code >= 400:
            detail = response.text[:500]
            try:
                detail = response.json().get("detail") or detail
            except Exception:
                pass
            raise RuntimeError(
                f"shared_transcript_http_{response.status_code}:{detail}"
            )

        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("shared_transcript_invalid_response")
        return payload

    def infer(self, *, stage: str, prompt: str) -> str:
        """Run one stateless NC inference turn on the production cloud brain."""
        normalized_stage = str(stage or "").strip().lower()
        if normalized_stage not in {"summary", "analysis", "voice"}:
            raise ValueError("invalid_nc_inference_stage")

        prompt = str(prompt or "")
        if not prompt.strip():
            raise ValueError("empty_nc_inference_prompt")

        result = self._json(
            "POST",
            "/internal/nc/infer",
            body={
                "stage": normalized_stage,
                "prompt": prompt,
            },
            timeout=max(self.timeout, 120.0),
        )

        if not result.get("ok"):
            raise RuntimeError("shared_nc_inference_failed")

        text = result.get("text")
        if not isinstance(text, str) or not text.strip():
            raise RuntimeError("shared_nc_inference_empty_response")

        return text.strip()

    def emit_trace(
        self,
        *,
        run_id: str,
        day_id: str,
        seq: int,
        event: str,
        stage: str | None = None,
        status: str | None = None,
        title: str | None = None,
        detail: str | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Publish one bounded, non-sensitive NC activity event."""
        body: dict[str, Any] = {
            "run_id": str(run_id),
            "day_id": str(day_id),
            "seq": int(seq),
            "event": str(event),
        }
        if stage is not None:
            body["stage"] = str(stage)
        if status is not None:
            body["status"] = str(status)
        if title is not None:
            body["title"] = str(title)
        if detail is not None:
            body["detail"] = str(detail)
        if metrics:
            body["metrics"] = dict(metrics)

        result = self._json(
            "POST",
            "/internal/nc/trace",
            body=body,
            timeout=min(self.timeout, 5.0),
        )
        if not result.get("ok"):
            raise RuntimeError("shared_nc_trace_failed")
        return result

    def pending_days(self, *, before_day_id: str | None = None) -> list[dict[str, Any]]:
        suffix = f"?before_day_id={before_day_id}" if before_day_id else ""
        result = self._json("GET", "/internal/transcript/pending" + suffix)
        days = result.get("days") or []
        return days if isinstance(days, list) else []

    def freeze(self, *, day_id: str | None = None, before_day_id: str | None = None) -> dict[str, Any] | None:
        result = self._json(
            "POST",
            "/internal/transcript/freeze",
            body={"day_id": day_id, "before_day_id": before_day_id},
        )
        if not result.get("pending", True):
            return None
        frozen = result.get("transcript")
        if not isinstance(frozen, dict):
            raise RuntimeError("shared_transcript_missing_payload")
        return frozen

    def acknowledge_and_purge(self, *, day_id: str, input_hash: str) -> dict[str, Any]:
        result = self._json(
            "POST",
            "/internal/transcript/processed",
            body={"day_id": day_id, "input_hash": input_hash, "purge": True},
        )
        if not result.get("ok"):
            raise RuntimeError("shared_transcript_ack_failed")
        return result
