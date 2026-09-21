"""Device transport contract and a bounded, non-networked subprocess implementation."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from typing import Protocol
import uuid

from .devices import Device, DeviceRegistry

MAX_FRAME = 65536


class DeviceTransport(Protocol):
    device: Device

    def execute(self, capability: str, arguments: dict, *, confirmed=False, request_id=None) -> dict: ...
    def refresh(self) -> bool: ...
    def close(self) -> None: ...


def worker_environment():
    # Explicit OS integration environment; no API keys, dotenv, Python injection,
    # memory/sensor paths, model endpoints or parent authentication tokens.
    allowed = {"SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC", "PATH", "PATHEXT",
               "TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "PROGRAMDATA",
               "PROGRAMFILES", "PROGRAMFILES(X86)", "EPIS_DEVICE_ID", "EPIS_DEVICE_NAME"}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


class StdioDeviceAgent:
    """Owned child process and private pipe handles; not an OS sandbox or remote service."""
    def __init__(self, registry: DeviceRegistry, timeout=15.0):
        self.registry = registry
        self.timeout = timeout
        self.device = None
        self._closed = False
        self._lock = threading.Lock()
        self._responses = queue.Queue(maxsize=4)
        worker = Path(__file__).with_name("device_worker.py")
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(worker)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env=worker_environment(), cwd=str(worker.parent),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        try:
            description = self._request({"operation": "describe"})
            if not description.get("ok"):
                raise RuntimeError("Device handshake failed")
            raw = description["device"]
            raw["capabilities"] = set(raw["capabilities"])
            self.device = registry.register(Device(**raw))
        except Exception:
            self.close()
            raise

    def _read(self):
        try:
            while not self._closed:
                line = self.process.stdout.readline(MAX_FRAME + 1)
                if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
                    break
                self._responses.put_nowait(json.loads(line))
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self._responses.put_nowait(None)
            except queue.Full:
                pass

    def _request(self, payload, timeout=None):
        with self._lock:
            if self._closed or self.process.poll() is not None:
                raise ConnectionError("Device offline")
            envelope = {"version": 1, "id": str(uuid.uuid4()), **payload}
            encoded = (json.dumps(envelope, ensure_ascii=False) + "\n").encode("utf-8")
            if len(encoded) > MAX_FRAME:
                raise ValueError("Device request too large")
            self.process.stdin.write(encoded)
            self.process.stdin.flush()
            response = self._responses.get(timeout=self.timeout if timeout is None else timeout)
            if (not isinstance(response, dict) or response.get("version") != 1
                    or response.get("id") != envelope["id"] or not isinstance(response.get("result"), dict)
                    or type(response["result"].get("ok")) is not bool):
                raise ConnectionError("Invalid device response")
            return response["result"]

    def execute(self, capability, arguments, *, confirmed=False, request_id=None):
        try:
            timeout = 30 if capability == "shell.powershell" else self.timeout
            return self._request({"operation": "execute", "id": request_id or str(uuid.uuid4()),
                                  "device_id": self.device.device_id, "capability": capability,
                                  "arguments": arguments, "confirmed": confirmed,
                                  "deadline": time.time() + min(timeout, 30)}, timeout=timeout)
        except (OSError, ValueError, queue.Empty):
            self.close()
            return {"ok": False, "outcome": "unknown",
                    "error": "Device connection lost; execution unknown. Do not retry automatically."}

    def refresh(self):
        if self.device:
            self.device.online = not self._closed and self.process.poll() is None
        return bool(self.device and self.device.online)

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.device:
            self.device.online = False
        # Only this owned helper, never an application targeted by an EPIS tool.
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        for stream in (self.process.stdin, self.process.stdout):
            if stream:
                stream.close()
        self._reader.join(timeout=1)
