from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hmac
import hashlib
import os
import re
import uuid
from typing import Any, Callable, Coroutine

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from agentic.cli import create_core
from agentic.cloud_transport import CloudDeviceTransport
from agentic.devices import Device


SERVER_VERSION = "0.6.0"
DEPLOYMENT_MODE = os.getenv("EPIS_DEPLOYMENT", "local").strip().lower()
SERVER_TOKEN = os.getenv("EPIS_SERVER_TOKEN", "").strip()
WEBHOOK_SHARED_SECRET = os.getenv("WEBHOOK_SHARED_SECRET", "").strip()
INTERNAL_EVENT_TOKEN = os.getenv("EPIS_INTERNAL_EVENT_TOKEN", "").strip()

if DEPLOYMENT_MODE == "cloud" and not SERVER_TOKEN:
    raise RuntimeError("EPIS_SERVER_TOKEN is required in cloud mode")


app = FastAPI(
    title="EPIS Server",
    version=SERVER_VERSION,
)


# Tek process = tek EPIS beyni. Desktop/mobile ayni shared session'i kullanir,
# ancak model/tool turn'leri birbirini gereksiz yere bloklamaz.
_core = None
_MAX_CONCURRENT_TURNS = max(
    1,
    min(
        int(os.getenv("EPIS_MAX_CONCURRENT_TURNS", "4")),
        8,
    ),
)
_core_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_TURNS)
_device_lock = asyncio.Lock()
_client_lock = asyncio.Lock()
_request_lock = asyncio.Lock()
_approval_claim_lock = asyncio.Lock()
_operation_lock = asyncio.Lock()
_client_connections: dict[str, tuple[WebSocket, asyncio.Lock]] = {}
_seen_request_ids: dict[str, str] = {}
_approval_claims: set[str] = set()
_processing_operations: set[str] = set()
_background_tasks: set[asyncio.Task] = set()
_whatsapp_pending: dict[str, str] = {}
_REQUEST_CACHE_LIMIT = 4096
_ID_RE = re.compile(r"[a-zA-Z0-9._:-]{1,160}")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _valid_id(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not _ID_RE.fullmatch(value):
        return None
    return value


def _bearer_or_alt_secret(request: Request, alt_header: str) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get(alt_header) or "").strip()


def _require_shared_secret(
    request: Request,
    expected: str,
    *,
    alt_header: str,
    missing_error: str,
) -> None:
    if not expected:
        if DEPLOYMENT_MODE == "cloud":
            raise HTTPException(status_code=503, detail=missing_error)
        return
    supplied = _bearer_or_alt_secret(request, alt_header)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Unauthorized")


def _whatsapp_client_id(sender: str) -> str:
    digest = hashlib.sha256(sender.encode("utf-8", errors="ignore")).hexdigest()[:24]
    return f"whatsapp:{digest}"


async def _register_client(
    client_id: str,
    websocket: WebSocket,
) -> None:
    async with _client_lock:
        _client_connections[client_id] = (
            websocket,
            asyncio.Lock(),
        )


async def _unregister_client(
    client_id: str | None,
    websocket: WebSocket,
) -> None:
    if not client_id:
        return
    async with _client_lock:
        current = _client_connections.get(client_id)
        if current is not None and current[0] is websocket:
            _client_connections.pop(client_id, None)


async def _send_to_client(
    client_id: str,
    payload: dict[str, Any],
) -> bool:
    async with _client_lock:
        current = _client_connections.get(client_id)
    if current is None:
        return False
    websocket, send_lock = current
    try:
        async with send_lock:
            await websocket.send_json(payload)
        return True
    except Exception:
        return False


async def _broadcast_clients(
    payload: dict[str, Any],
) -> None:
    async with _client_lock:
        client_ids = list(_client_connections)
    await asyncio.gather(
        *(
            _send_to_client(client_id, payload)
            for client_id in client_ids
        ),
        return_exceptions=True,
    )


async def _claim_request(
    request_id: str,
    client_id: str,
) -> bool:
    async with _request_lock:
        if request_id in _seen_request_ids:
            return False
        _seen_request_ids[request_id] = client_id
        while len(_seen_request_ids) > _REQUEST_CACHE_LIMIT:
            oldest = next(iter(_seen_request_ids))
            _seen_request_ids.pop(oldest, None)
        return True


async def _claim_approval(
    approval_id: str,
) -> bool:
    async with _approval_claim_lock:
        if approval_id in _approval_claims:
            return False
        _approval_claims.add(approval_id)
        return True


async def _release_approval(
    approval_id: str,
) -> None:
    async with _approval_claim_lock:
        _approval_claims.discard(approval_id)


async def _begin_operation(operation_id: str) -> None:
    async with _operation_lock:
        _processing_operations.add(operation_id)


async def _end_operation(operation_id: str) -> None:
    async with _operation_lock:
        _processing_operations.discard(operation_id)


async def _has_processing_operations() -> bool:
    async with _operation_lock:
        return bool(_processing_operations)


def _spawn_background(
    coroutine: Coroutine[Any, Any, Any],
) -> None:
    task = asyncio.create_task(coroutine)
    _background_tasks.add(task)
    task.add_done_callback(
        _background_tasks.discard
    )


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
    return [
        device
        for device in core.public_devices()
        if device.get("platform") != "cloud"
    ]


def jsonable(value: Any) -> Any:
    """Convert AgentCore/tool results into JSON-safe values."""
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


def turn_payload(
    turn: Any,
    *,
    request_id: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "assistant.message",
        "request_id": request_id,
        "client_id": client_id,
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
    """Run synchronous Core work without blocking the event loop.

    Phase 2 allows a bounded number of shared-session turns to progress in
    parallel. AgentCore protects only its short shared-state critical sections.
    """
    async with _core_semaphore:
        return await asyncio.to_thread(function, *args)


async def _broadcast_devices_snapshot() -> None:
    """Push public device metadata after connect/disconnect state changes."""
    try:
        payload = await run_core_call(devices_payload)
    except Exception:
        return
    await _broadcast_clients(
        {
            "type": "devices.snapshot",
            "request_id": None,
            "devices": payload,
            "time": utc_now(),
        }
    )


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


async def _whatsapp_turn(data: dict[str, Any]) -> dict[str, Any]:
    body = str(data.get("body") or data.get("text") or "").strip()
    if not body:
        return {"reply": None}

    sender = str(data.get("from") or data.get("sender") or "default").strip() or "default"
    client_id = _whatsapp_client_id(sender)
    raw_message_id = str(data.get("message_id") or data.get("id") or "").strip()
    request_id = _valid_id(raw_message_id) or f"wa:{uuid.uuid4().hex}"
    origin_device_id = f"whatsapp:{client_id.split(':', 1)[1]}"
    folded = re.sub(r"\s+", " ", body.casefold()).strip()

    core = get_core()
    pending_id = _whatsapp_pending.get(client_id)
    confirm_words = {"onayla", "evet onayla", "approve", "confirm"}
    reject_words = {"reddet", "hayir reddet", "hayır reddet", "reject", "cancel", "iptal"}

    if pending_id and folded in confirm_words | reject_words:
        function = core.confirm_pending if folded in confirm_words else core.reject_pending
        turn = await run_core_call(function, pending_id, client_id)
    else:
        turn = await run_core_call(
            core.handle,
            body,
            request_id,
            client_id,
            origin_device_id,
        )

    confirmation_required = bool(getattr(turn, "confirmation_required", False))
    approval = jsonable(getattr(turn, "approval", None))
    approval_id = None
    if isinstance(approval, dict):
        approval_id = _valid_id(approval.get("id") or approval.get("approval_id"))

    if confirmation_required and approval_id:
        _whatsapp_pending[client_id] = approval_id
    else:
        _whatsapp_pending.pop(client_id, None)

    reply = str(getattr(turn, "message", "") or "").strip()
    if not reply and confirmation_required and isinstance(approval, dict):
        reply = str(approval.get("message") or "").strip()
    if confirmation_required and reply:
        reply += "\n\nWhatsApp'tan onaylamak icin 'onayla', iptal etmek icin 'reddet' yazabilirsin."

    event = {
        "type": "session.external_turn",
        "source": "whatsapp",
        "request_id": request_id,
        "client_id": client_id,
        "user_text": body,
        "assistant_text": reply,
        "confirmation_required": confirmation_required,
        "time": utc_now(),
    }
    await _broadcast_clients(event)

    return {
        "reply": reply or None,
        "request_id": request_id,
        "confirmation_required": confirmation_required,
        "approval_id": approval_id,
    }


@app.post("/whatsapp/incoming")
@app.post("/integrations/whatsapp/incoming")
async def whatsapp_incoming(request: Request):
    _require_shared_secret(
        request,
        WEBHOOK_SHARED_SECRET,
        alt_header="x-epis-webhook-secret",
        missing_error="WEBHOOK_SHARED_SECRET is required in cloud mode",
    )
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")
    return JSONResponse(await _whatsapp_turn(data))


@app.post("/internal/proactive")
async def proactive_event(request: Request):
    _require_shared_secret(
        request,
        INTERNAL_EVENT_TOKEN,
        alt_header="x-epis-internal-token",
        missing_error="EPIS_INTERNAL_EVENT_TOKEN is required in cloud mode",
    )
    try:
        data = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid_json") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="invalid_payload")

    trigger_type = str(data.get("trigger_type") or "event").strip()[:80] or "event"
    context = str(data.get("context") or "").strip()
    priority = str(data.get("priority") or "medium").strip()[:32] or "medium"
    request_id = _valid_id(data.get("request_id")) or f"kairos:{uuid.uuid4().hex}"
    if not context:
        raise HTTPException(status_code=400, detail="context_required")
    if len(context) > 12000:
        raise HTTPException(status_code=413, detail="context_too_large")

    core = get_core()
    try:
        turn = await run_core_call(
            core.handle_internal_event,
            trigger_type,
            context,
            priority,
            request_id,
            "kairos",
            "trusted-local-event",
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=public_error_detail(exc),
        ) from exc

    text = str(getattr(turn, "message", "") or "").strip()
    async with _client_lock:
        connected_clients = len(_client_connections)
    await _broadcast_clients(
        {
            "type": "proactive.message",
            "request_id": request_id,
            "trigger_type": trigger_type,
            "priority": priority,
            "text": text,
            "time": utc_now(),
        }
    )
    return JSONResponse(
        {
            "ok": True,
            "request_id": request_id,
            "message": text,
            "delivered_clients": connected_clients,
            "stored_in_shared_session": True,
        }
    )


async def _process_chat(
    client_id: str,
    origin_device_id: str,
    request_id: str,
    text: str,
) -> None:
    core = get_core()
    try:
        turn = await run_core_call(
            core.handle,
            text,
            request_id,
            client_id,
            origin_device_id,
        )
    except Exception as exc:
        await _send_to_client(
            client_id,
            {
                "type": "error",
                "request_id": request_id,
                "error": "agent_error",
                "detail": public_error_detail(exc),
                "time": utc_now(),
            },
        )
    else:
        await _send_to_client(
            client_id,
            turn_payload(
                turn,
                request_id=request_id,
                client_id=client_id,
            ),
        )
    finally:
        await _end_operation(request_id)


async def _process_approval(
    *,
    client_id: str,
    approval_id: str,
    action: str,
    request_id: str,
    operation_id: str,
) -> None:
    core = get_core()
    try:
        function = (
            core.confirm_pending
            if action == "confirm"
            else core.reject_pending
        )
        turn = await run_core_call(
            function,
            approval_id,
            client_id,
        )
    except Exception as exc:
        await _send_to_client(
            client_id,
            {
                "type": "error",
                "request_id": request_id,
                "approval_id": approval_id,
                "error": (
                    "confirmation_error"
                    if action == "confirm"
                    else "rejection_error"
                ),
                "detail": public_error_detail(exc),
                "time": utc_now(),
            },
        )
    else:
        await _send_to_client(
            client_id,
            turn_payload(
                turn,
                request_id=request_id,
                client_id=client_id,
            ),
        )
    finally:
        await _release_approval(approval_id)
        await _end_operation(operation_id)


async def _process_conversation_reset(
    client_id: str,
    request_id: str,
) -> None:
    core = get_core()
    if await _has_processing_operations():
        await _send_to_client(
            client_id,
            {
                "type": "error",
                "request_id": request_id,
                "error": "new_context_error",
                "detail": "conversation_busy",
                "time": utc_now(),
            },
        )
        return
    try:
        cleared = await run_core_call(
            core.new_conversation
        )
    except RuntimeError as exc:
        detail = (
            "conversation_busy"
            if str(exc) == "conversation_busy"
            else public_error_detail(exc)
        )
        await _send_to_client(
            client_id,
            {
                "type": "error",
                "request_id": request_id,
                "error": "new_context_error",
                "detail": detail,
                "time": utc_now(),
            },
        )
        return
    except Exception as exc:
        await _send_to_client(
            client_id,
            {
                "type": "error",
                "request_id": request_id,
                "error": "new_context_error",
                "detail": public_error_detail(exc),
                "time": utc_now(),
            },
        )
        return

    await _broadcast_clients(
        {
            "type": "conversation.reset",
            "request_id": request_id,
            "cleared_messages": int(cleared),
            "ok": True,
            "time": utc_now(),
        }
    )


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

    client_id: str | None = None
    client_type: str | None = None

    await websocket.send_json(
        {
            "type": "connected",
            "service": "epis",
            "version": SERVER_VERSION,
            "protocol_version": 2,
            "deployment": DEPLOYMENT_MODE,
            "time": utc_now(),
        }
    )

    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": "invalid_message",
                        "time": utc_now(),
                    }
                )
                continue

            message_type = message.get("type")

            if message_type == "ping":
                payload = {
                    "type": "pong",
                    "time": utc_now(),
                }
                if client_id:
                    await _send_to_client(
                        client_id,
                        payload,
                    )
                else:
                    await websocket.send_json(
                        payload
                    )
                continue

            if message_type == "client.hello":
                if client_id is not None:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "error": "client_already_ready",
                            "time": utc_now(),
                        },
                    )
                    continue

                hello_client_id = _valid_id(
                    message.get("client_id")
                )
                hello_type = str(
                    message.get("client_type") or ""
                ).strip().lower()

                if (
                    message.get("version") != 2
                    or hello_client_id is None
                    or hello_type
                    not in {
                        "desktop",
                        "mobile",
                        "web",
                        "whatsapp",
                    }
                ):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "error": "invalid_client_hello",
                            "time": utc_now(),
                        }
                    )
                    continue

                client_id = hello_client_id
                client_type = hello_type
                await _register_client(
                    client_id,
                    websocket,
                )
                await _send_to_client(
                    client_id,
                    {
                        "type": "client.ready",
                        "client_id": client_id,
                        "client_type": client_type,
                        "protocol_version": 2,
                        "time": utc_now(),
                    },
                )
                continue

            # Protocol-v1 compatibility for the existing read-only usage
            # snapshot request. Chat/actions still require client.hello so
            # request/approval routing always has a stable client identity.
            if client_id is None and message_type == "usage.get":
                request_id = _valid_id(
                    message.get("request_id")
                )
                try:
                    hours = int(
                        message.get("hours", 24)
                    )
                    limit = int(
                        message.get("limit", 20)
                    )
                    payload = await run_core_call(
                        usage_payload,
                        hours,
                        limit,
                    )
                except (TypeError, ValueError):
                    await websocket.send_json(
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "invalid_usage_request",
                            "time": utc_now(),
                        }
                    )
                    continue
                except Exception as exc:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "usage_unavailable",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        }
                    )
                    continue

                await websocket.send_json(
                    {
                        "type": "usage.snapshot",
                        "request_id": request_id,
                        "data": payload,
                        "time": utc_now(),
                    }
                )
                continue

            if client_id is None:
                await websocket.send_json(
                    {
                        "type": "error",
                        "error": "client_hello_required",
                        "time": utc_now(),
                    }
                )
                continue

            if message_type == "chat.send":
                request_id = _valid_id(
                    message.get("request_id")
                )
                text = str(
                    message.get("text", "")
                ).strip()

                if request_id is None:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "error": "invalid_request_id",
                            "time": utc_now(),
                        },
                    )
                    continue

                if not text:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "empty_message",
                            "time": utc_now(),
                        },
                    )
                    continue

                if not await _claim_request(
                    request_id,
                    client_id,
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "duplicate_request_id",
                            "time": utc_now(),
                        },
                    )
                    continue

                origin_device_id = _valid_id(
                    message.get(
                        "origin_device_id"
                    )
                ) or (
                    f"{client_type}:{client_id}"
                )

                await _begin_operation(request_id)
                await _send_to_client(
                    client_id,
                    {
                        "type": "chat.accepted",
                        "request_id": request_id,
                        "client_id": client_id,
                        "time": utc_now(),
                    },
                )
                _spawn_background(
                    _process_chat(
                        client_id,
                        origin_device_id,
                        request_id,
                        text,
                    )
                )
                continue

            if message_type == "usage.get":
                request_id = _valid_id(
                    message.get("request_id")
                )
                try:
                    hours = int(
                        message.get("hours", 24)
                    )
                    limit = int(
                        message.get("limit", 20)
                    )
                    payload = await run_core_call(
                        usage_payload,
                        hours,
                        limit,
                    )
                except (TypeError, ValueError):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "invalid_usage_request",
                            "time": utc_now(),
                        },
                    )
                    continue
                except Exception as exc:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "usage_unavailable",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        },
                    )
                    continue

                await _send_to_client(
                    client_id,
                    {
                        "type": "usage.snapshot",
                        "request_id": request_id,
                        "data": payload,
                        "time": utc_now(),
                    },
                )
                continue

            if message_type == "devices.get":
                request_id = _valid_id(
                    message.get("request_id")
                )
                try:
                    payload = await run_core_call(
                        devices_payload
                    )
                except Exception as exc:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "devices_unavailable",
                            "detail": public_error_detail(exc),
                            "time": utc_now(),
                        },
                    )
                    continue

                await _send_to_client(
                    client_id,
                    {
                        "type": "devices.snapshot",
                        "request_id": request_id,
                        "devices": payload,
                        "time": utc_now(),
                    },
                )
                continue

            if message_type in {
                "approval.confirm",
                "approval.reject",
            }:
                approval_id = _valid_id(
                    message.get("approval_id")
                )
                operation_id = _valid_id(
                    message.get("operation_id")
                )
                action = (
                    "confirm"
                    if message_type
                    == "approval.confirm"
                    else "reject"
                )

                if (
                    approval_id is None
                    or operation_id is None
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "error": "invalid_approval_request",
                            "time": utc_now(),
                        },
                    )
                    continue

                metadata = get_core().pending_metadata(
                    approval_id
                )
                if (
                    metadata is None
                    or metadata.get("client_id")
                    != client_id
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "operation_id": operation_id,
                            "approval_id": approval_id,
                            "error": "approval_not_owned_or_expired",
                            "time": utc_now(),
                        },
                    )
                    continue

                if not await _claim_request(
                    operation_id,
                    client_id,
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "operation_id": operation_id,
                            "approval_id": approval_id,
                            "error": "duplicate_operation_id",
                            "time": utc_now(),
                        },
                    )
                    continue

                if not await _claim_approval(
                    approval_id
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "operation_id": operation_id,
                            "approval_id": approval_id,
                            "error": "approval_already_processing",
                            "time": utc_now(),
                        },
                    )
                    continue

                original_request_id = str(
                    metadata.get("request_id")
                    or ""
                )
                await _begin_operation(operation_id)
                await _send_to_client(
                    client_id,
                    {
                        "type": "approval.accepted",
                        "operation_id": operation_id,
                        "approval_id": approval_id,
                        "request_id": original_request_id,
                        "action": action,
                        "time": utc_now(),
                    },
                )
                _spawn_background(
                    _process_approval(
                        client_id=client_id,
                        approval_id=approval_id,
                        action=action,
                        request_id=(
                            original_request_id
                        ),
                        operation_id=operation_id,
                    )
                )
                continue

            if message_type == "conversation.new":
                request_id = _valid_id(
                    message.get("request_id")
                )
                if request_id is None:
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "error": "invalid_request_id",
                            "time": utc_now(),
                        },
                    )
                    continue

                if not await _claim_request(
                    request_id,
                    client_id,
                ):
                    await _send_to_client(
                        client_id,
                        {
                            "type": "error",
                            "request_id": request_id,
                            "error": "duplicate_request_id",
                            "time": utc_now(),
                        },
                    )
                    continue

                await _send_to_client(
                    client_id,
                    {
                        "type": "conversation.accepted",
                        "request_id": request_id,
                        "time": utc_now(),
                    },
                )
                _spawn_background(
                    _process_conversation_reset(
                        client_id,
                        request_id,
                    )
                )
                continue

            await _send_to_client(
                client_id,
                {
                    "type": "error",
                    "error": "unsupported_message_type",
                    "received_type": message_type,
                    "time": utc_now(),
                },
            )

    except WebSocketDisconnect:
        pass
    finally:
        await _unregister_client(
            client_id,
            websocket,
        )


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

        async with _device_lock:
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
        await _broadcast_devices_snapshot()

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
            async with _device_lock:
                core.detach_transport(device_id, transport)
            await _broadcast_devices_snapshot()
