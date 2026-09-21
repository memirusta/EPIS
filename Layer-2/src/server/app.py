from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hmac
import os
import re
from typing import Any, Callable

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from agentic.cli import create_core
from agentic.cloud_transport import CloudDeviceTransport
from agentic.devices import Device


SERVER_VERSION = "0.4.0"
DEPLOYMENT_MODE = os.getenv("EPIS_DEPLOYMENT", "local").strip().lower()
SERVER_TOKEN = os.getenv("EPIS_SERVER_TOKEN", "").strip()

if DEPLOYMENT_MODE == "cloud" and not SERVER_TOKEN:
    raise RuntimeError("EPIS_SERVER_TOKEN is required in cloud mode")


app = FastAPI(
    title="EPIS Server",
    version=SERVER_VERSION,
)


# Tek process = tek EPIS beyni.
# Desktop ve mobile ayn? AgentCore state'ini payla?acak.
_core = None
_core_lock = asyncio.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def authorized_subprotocol(
    header: str | None,
    expected_token: str,
    protocol: str = "epis",
) -> str | None:
    """Validate the browser-compatible WebSocket subprotocol credentials."""
    if not expected_token:
        return None
    protocols = {
        item.strip()
        for item in (header or "").split(",")
        if item.strip()
    }
    if protocol not in protocols:
        return None
    return (
        protocol
        if any(hmac.compare_digest(item, expected_token) for item in protocols)
        else None
    )


def device_from_hello(message: Any, allowed_capabilities: set[str]) -> Device:
    if not isinstance(message, dict) or message.get("type") != "device.hello":
        raise ValueError("device_hello_required")
    if message.get("version") != 1 or not isinstance(message.get("device"), dict):
        raise ValueError("invalid_device_hello")
    raw = message["device"]
    device_id = raw.get("device_id")
    display_name = raw.get("display_name")
    platform = raw.get("platform")
    capabilities = raw.get("capabilities")
    if not isinstance(device_id, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,80}", device_id):
        raise ValueError("invalid_device_id")
    if not isinstance(display_name, str) or not display_name.strip() or len(display_name) > 120:
        raise ValueError("invalid_device_name")
    if platform != "windows":
        raise ValueError("unsupported_device_platform")
    if (
        not isinstance(capabilities, list)
        or len(capabilities) > 128
        or any(not isinstance(item, str) for item in capabilities)
    ):
        raise ValueError("invalid_device_capabilities")
    requested = set(capabilities)
    if not requested.issubset(allowed_capabilities):
        raise ValueError("unsupported_device_capability")
    return Device(
        device_id=device_id,
        display_name=display_name.strip(),
        platform=platform,
        capabilities=requested,
        online=True,
        sensitive_state_local=True,
    )


def public_error_detail(exc: Exception) -> str:
    if DEPLOYMENT_MODE == "cloud":
        return "internal_error"
    return type(exc).__name__


def get_core():
    global _core

    if _core is None:
        _core = create_core()

    return _core


def usage_payload(hours: int = 24, limit: int = 20) -> dict[str, Any]:
    """Return OpenAI totals plus local metadata; never prompts or replies."""
    core = get_core()
    repository = getattr(core, "usage_repository", None)
    if repository is None:
        raise RuntimeError("usage_repository_unavailable")
    return repository.snapshot(hours=hours, limit=limit)


def devices_payload() -> list[dict[str, Any]]:
    """Return connection metadata only; never device state or private content."""
    core = get_core()
    for transport in core.transports.values():
        transport.refresh()
    return [
        device
        for device in core.devices.list_public()
        if device.get("platform") != "cloud"
    ]


def jsonable(value: Any) -> Any:
    """AgentCore/tool sonu?lar?n? g?venli JSON de?erlerine d?n??t?r."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value

    if is_dataclass(value):
        return jsonable(asdict(value))

    if isinstance(value, dict):
        return {
            str(key): jsonable(item)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]

    return str(value)


def turn_payload(turn: Any) -> dict[str, Any]:
    return {
        "type": "assistant.message",
        "text": getattr(turn, "message", str(turn)),
        "confirmation_required": bool(
            getattr(turn, "confirmation_required", False)
        ),
        "approval": jsonable(
            getattr(turn, "approval", None)
        ),
        "tool_results": jsonable(
            getattr(turn, "tool_results", [])
        ),
        "time": utc_now(),
    }


async def run_core_call(
    function: Callable[..., Any],
    *args: Any,
) -> Any:
    """
    AgentCore senkron ?al???yor ve OpenAI/tool ?a?r?lar? uzun s?rebilir.

    Event loop'u bloke etmemek ve ayn? AgentCore ?zerinde iki cihaz?n
    e?zamanl? state de?i?tirmesini engellemek i?in ?a?r?lar? s?raya al.
    """
    async with _core_lock:
        return await asyncio.to_thread(function, *args)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "epis",
        "version": SERVER_VERSION,
        "deployment": DEPLOYMENT_MODE,
        "authentication": "required" if SERVER_TOKEN else "local-only",
        "time": utc_now(),
    }


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket) -> None:
    selected_protocol = authorized_subprotocol(
        websocket.headers.get("sec-websocket-protocol"),
        SERVER_TOKEN,
    )
    if SERVER_TOKEN and selected_protocol is None:
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept(subprotocol=selected_protocol)

    await websocket.send_json(
        {
            "type": "connected",
            "service": "epis",
            "version": SERVER_VERSION,
            "deployment": DEPLOYMENT_MODE,
            "time": utc_now(),
        }
    )

    try:
        while True:
            message = await websocket.receive_json()
            message_type = message.get("type")

            if message_type == "ping":
                await websocket.send_json(
                    {
                        "type": "pong",
                        "time": utc_now(),
                    }
                )
                continue

            if message_type == "chat.send":
                text = str(message.get("text", "")).strip()

                if not text:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "empty_message",
                            "time": utc_now(),
                        }
                    )
                    continue

                core = get_core()

                try:
                    turn = await run_core_call(core.handle, text)
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "agent_error",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(turn_payload(turn))
                continue

            if message_type == "usage.get":
                try:
                    hours = int(message.get("hours", 24))
                    limit = int(message.get("limit", 20))
                    payload = await run_core_call(
                        usage_payload,
                        hours,
                        limit,
                    )
                except (TypeError, ValueError):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "invalid_usage_request",
                            "time": utc_now(),
                        }
                    )
                    continue
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "usage_unavailable",
                            "detail": type(exc).__name__,
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "usage.snapshot",
                        "data": payload,
                        "time": utc_now(),
                    }
                )
                continue

            if message_type == "devices.get":
                try:
                    payload = await run_core_call(devices_payload)
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "devices_unavailable",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "devices.snapshot",
                        "devices": payload,
                        "time": utc_now(),
                    }
                )
                continue

            if message_type == "approval.confirm":
                core = get_core()
                approval_id = message.get("approval_id")

                try:
                    turn = await run_core_call(
                        core.confirm_pending,
                        approval_id,
                    )
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "confirmation_error",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(turn_payload(turn))
                continue

            if message_type == "approval.reject":
                core = get_core()
                approval_id = message.get("approval_id")

                try:
                    turn = await run_core_call(
                        core.reject_pending,
                        approval_id,
                    )
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "rejection_error",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(turn_payload(turn))
                continue

            if message_type == "conversation.new":
                core = get_core()

                try:
                    await run_core_call(core.new_conversation)
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "new_context_error",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "conversation.reset",
                        "ok": True,
                        "time": utc_now(),
                    }
                )
                continue

            await websocket.send_json(
                {
                    "type": "error",
                    "error": "unsupported_message_type",
                    "received_type": message_type,
                    "time": utc_now(),
                }
            )

    except WebSocketDisconnect:
        pass


@app.websocket("/device/ws")
async def device_websocket_endpoint(websocket: WebSocket) -> None:
    selected_protocol = authorized_subprotocol(
        websocket.headers.get("sec-websocket-protocol"),
        SERVER_TOKEN,
        "epis-device",
    )
    if SERVER_TOKEN and selected_protocol is None:
        await websocket.close(code=1008, reason="unauthorized")
        return

    await websocket.accept(subprotocol=selected_protocol)
    transport = None
    core = None
    device_id = None

    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15.0)
        core = get_core()
        device = device_from_hello(
            hello,
            core.registry.capabilities("windows"),
        )
        device_id = device.device_id
        transport = CloudDeviceTransport(
            websocket,
            device,
            asyncio.get_running_loop(),
        )

        async with _core_lock:
            existing = core.transports.get(device_id)
            if existing is not None and existing is not core.local_agent:
                core.detach_transport(device_id, existing)
            core.attach_transport(transport)

        await websocket.send_json({
            "type": "device.connected",
            "device_id": device_id,
            "capabilities": sorted(device.capabilities),
            "time": utc_now(),
        })

        while True:
            message = await websocket.receive_json()
            if isinstance(message, dict) and message.get("type") == "device.pong":
                device.online = True
                continue
            if not transport.handle_message(message):
                await websocket.send_json({
                    "type": "device.error",
                    "error": "invalid_device_message",
                    "time": utc_now(),
                })

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except ValueError as exc:
        await websocket.close(code=1008, reason=str(exc)[:120])
    finally:
        if core is not None and transport is not None and device_id is not None:
            async with _core_lock:
                core.detach_transport(device_id, transport)
