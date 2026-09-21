"""Outbound Windows device agent for a remote EPIS control plane."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import random
import sys
import tempfile
from urllib.parse import urlsplit, urlunsplit

# Bundled direct entrypoint. Do not load dotenv or model credentials.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentic.device_worker import DeviceWorker


logger = logging.getLogger("EPIS.DEVICE")
MAX_MESSAGE_BYTES = 2 * 1024 * 1024


def device_url(server_url: str) -> str:
    parsed = urlsplit(server_url.strip())
    if parsed.scheme not in {"ws", "wss"} or not parsed.netloc:
        raise ValueError("EPIS_SERVER_URL must be a ws:// or wss:// URL")
    return urlunsplit((parsed.scheme, parsed.netloc, "/device/ws", "", ""))


def _connect():
    try:
        from websockets.asyncio.client import connect
    except ImportError:
        from websockets import connect
    return connect


def _acquire_single_instance():
    """Keep one Windows device agent per user session."""
    if os.name != "nt":
        return object()

    import msvcrt

    base = Path(os.getenv("LOCALAPPDATA") or tempfile.gettempdir()) / "EPIS"
    base.mkdir(parents=True, exist_ok=True)
    handle = open(base / "device-agent.lock", "a+b")

    try:
        if handle.seek(0, os.SEEK_END) == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None

    return handle


def _release_single_instance(handle) -> None:
    if handle is None or os.name != "nt":
        return
    try:
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    finally:
        handle.close()


async def run_once(url: str, token: str, worker: DeviceWorker) -> None:
    connect = _connect()
    async with connect(
        device_url(url),
        subprotocols=["epis-device", token],
        max_size=MAX_MESSAGE_BYTES,
        open_timeout=20,
        ping_interval=20,
        ping_timeout=20,
    ) as websocket:
        description = worker.handle({"version": 1, "operation": "describe"})
        if not description.get("ok"):
            raise RuntimeError("Device description unavailable")
        await websocket.send(json.dumps({
            "type": "device.hello",
            "version": 1,
            "device": description["device"],
        }, ensure_ascii=False))

        async for raw in websocket:
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > MAX_MESSAGE_BYTES:
                await websocket.close(code=1009, reason="message_too_large")
                return
            try:
                message = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(message, dict):
                continue
            if message.get("type") == "device.ping":
                await websocket.send(json.dumps({"type": "device.pong"}))
                continue
            if message.get("type") != "device.execute":
                continue
            command = {key: value for key, value in message.items() if key != "type"}
            command["operation"] = "execute"
            request_id = command.get("id")
            try:
                result = await asyncio.to_thread(worker.handle, command)
            except Exception:
                result = {
                    "ok": False,
                    "outcome": "unknown",
                    "error": "Device command failed; execution unknown",
                }
            await websocket.send(json.dumps({
                "type": "device.result",
                "version": 1,
                "id": request_id,
                "result": result,
            }, ensure_ascii=False))


async def run_forever(url: str, token: str) -> None:
    worker = DeviceWorker()
    delay = 1.0
    while True:
        try:
            await run_once(url, token, worker)
            delay = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Device connection unavailable: %s", type(exc).__name__)
        await asyncio.sleep(delay + random.random() * min(delay, 2.0))
        delay = min(delay * 2.0, 30.0)


def main() -> int:
    logging.basicConfig(level=logging.INFO)

    lease = _acquire_single_instance()
    if lease is None:
        logger.info("Another EPIS device agent is already running")
        return 73

    url = os.getenv("EPIS_SERVER_URL", "").strip()
    token = os.getenv("EPIS_SERVER_TOKEN", "").strip()
    if not url or not token:
        logger.error("EPIS_SERVER_URL and EPIS_SERVER_TOKEN are required")
        _release_single_instance(lease)
        return 2
    # Tool handlers and any subprocess they launch must never inherit the
    # control-plane credential. Reconnect state keeps only the in-memory copy.
    os.environ.pop("EPIS_SERVER_TOKEN", None)
    os.environ.pop("EPIS_SERVER_URL", None)
    try:
        asyncio.run(run_forever(url, token))
    except KeyboardInterrupt:
        return 0
    finally:
        _release_single_instance(lease)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
