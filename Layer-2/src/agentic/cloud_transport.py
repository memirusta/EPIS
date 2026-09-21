"""Server-side transport for an outbound, authenticated device WebSocket."""

from __future__ import annotations

import asyncio
import concurrent.futures
import time
import uuid
from typing import Any

from .devices import Device


class CloudDeviceTransport:
    """Expose a connected Windows device through the synchronous core contract.

    AgentCore executes in a worker thread. WebSocket I/O stays on FastAPI's event
    loop and pending request futures are completed only by the endpoint receive
    loop, so there is never more than one reader for the connection.
    """

    def __init__(
        self,
        websocket,
        device: Device,
        loop: asyncio.AbstractEventLoop,
        *,
        timeout: float = 30.0,
    ):
        self.websocket = websocket
        self.device = device
        self.loop = loop
        self.timeout = max(1.0, min(timeout, 45.0))
        self._send_lock = asyncio.Lock()
        self._pending: dict[str, asyncio.Future] = {}
        self._closed = False

    def execute(
        self,
        capability: str,
        arguments: dict,
        *,
        confirmed: bool = False,
        request_id: str | None = None,
    ) -> dict:
        if self._closed or not self.device.online:
            return {"ok": False, "error": "Device offline"}

        timeout = 45.0 if capability == "shell.powershell" else self.timeout
        future = asyncio.run_coroutine_threadsafe(
            self._execute_async(
                capability,
                arguments,
                confirmed=confirmed,
                request_id=request_id,
                timeout=timeout,
            ),
            self.loop,
        )
        try:
            result = future.result(timeout=timeout + 2.0)
        except (concurrent.futures.TimeoutError, TimeoutError):
            future.cancel()
            return {
                "ok": False,
                "outcome": "unknown",
                "error": "Device response timed out; execution unknown. Do not retry automatically.",
            }
        except Exception:
            return {
                "ok": False,
                "outcome": "unknown",
                "error": "Device connection lost; execution unknown. Do not retry automatically.",
            }
        return result if isinstance(result, dict) else {
            "ok": False,
            "outcome": "unknown",
            "error": "Invalid device response",
        }

    async def _execute_async(
        self,
        capability: str,
        arguments: dict,
        *,
        confirmed: bool,
        request_id: str | None,
        timeout: float,
    ) -> dict:
        if self._closed:
            raise ConnectionError("Device offline")
        command_id = request_id or str(uuid.uuid4())
        pending = self.loop.create_future()
        if command_id in self._pending:
            raise ValueError("Duplicate device command id")
        self._pending[command_id] = pending
        try:
            async with self._send_lock:
                await self.websocket.send_json({
                    "type": "device.execute",
                    "version": 1,
                    "id": command_id,
                    "device_id": self.device.device_id,
                    "capability": capability,
                    "arguments": arguments,
                    "confirmed": bool(confirmed),
                    "deadline": time.time() + min(timeout, 60.0),
                })
            return await asyncio.wait_for(pending, timeout=timeout)
        finally:
            self._pending.pop(command_id, None)

    def handle_message(self, message: Any) -> bool:
        if not isinstance(message, dict) or message.get("type") != "device.result":
            return False
        request_id = message.get("id")
        result = message.get("result")
        if not isinstance(request_id, str) or not isinstance(result, dict):
            return False
        if type(result.get("ok")) is not bool:
            return False
        pending = self._pending.get(request_id)
        if pending is None or pending.done():
            return False
        pending.set_result(result)
        self.device.online = True
        return True

    def refresh(self) -> bool:
        return not self._closed and self.device.online

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self.device.online = False

        def fail_pending() -> None:
            for pending in list(self._pending.values()):
                if not pending.done():
                    pending.set_exception(ConnectionError("Device disconnected"))
            self._pending.clear()

        if self.loop.is_running():
            self.loop.call_soon_threadsafe(fail_pending)
        else:
            fail_pending()
