"""Mutual TLS transport. Local-only harness; no public listening or implicit enrollment."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

from cryptography import x509

from .devices import Device
from .pairing import GrantStore, certificate_capabilities, server_context
from .transport import MAX_FRAME, worker_environment


class FramedTLS:
    def __init__(self, connection):
        self.connection = connection
        self.reader = connection.makefile("rb")

    def send(self, message):
        data = (json.dumps(message, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > MAX_FRAME:
            raise ValueError("Frame too large")
        self.connection.sendall(data)

    def receive(self):
        line = self.reader.readline(MAX_FRAME + 1)
        if not line or len(line) > MAX_FRAME or not line.endswith(b"\n"):
            raise ConnectionError("Invalid or closed frame")
        result = json.loads(line)
        if not isinstance(result, dict):
            raise ValueError("Frame must be an object")
        return result

    def close(self):
        try:
            self.connection.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.reader.close()
        self.connection.close()


class PairedTransport:
    """A single authenticated device; scoped grants are rechecked on every request."""
    def __init__(self, connection, grants, registry, *, timeout=5.0, heartbeat_interval=5.0):
        self.grants = grants
        self.connection = connection
        self.connection.settimeout(timeout)
        self.frame = FramedTLS(connection)
        self.fingerprint = hashlib.sha256(connection.getpeercert(binary_form=True)).hexdigest()
        self.certificate = x509.load_der_x509_certificate(connection.getpeercert(binary_form=True))
        self.device = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._heartbeat_thread = None
        self.timeout = timeout
        self.heartbeat_interval = heartbeat_interval
        try:
            grant = self.grants.get(self.fingerprint)
            if not grant:
                raise PermissionError("Device is not enrolled or grant expired/revoked")
            result = self._request({"operation": "describe"})
            raw = result["device"]
            if raw["device_id"] != grant["device_id"]:
                raise PermissionError("Device certificate identity mismatch")
            caps = set(raw["capabilities"]) & grant["capabilities"] & certificate_capabilities(self.certificate)
            self.device = registry.register(Device(grant["device_id"], str(raw["display_name"])[:128],
                                                   str(raw["platform"])[:32], caps))
            self._heartbeat_thread = threading.Thread(target=self._heartbeat, daemon=True)
            self._heartbeat_thread.start()
        except Exception:
            self.close()
            raise

    def _request(self, payload, timeout=None):
        with self._lock:
            grant = self.grants.get(self.fingerprint)
            if (self._stop.is_set() or not grant
                    or self.certificate.not_valid_after_utc.timestamp() <= time.time()
                    or (self.device and self.device.device_id != grant["device_id"])):
                raise PermissionError("Device grant unavailable")
            request = {"version": 1, "id": str(uuid.uuid4()), **payload}
            self.connection.settimeout(self.timeout if timeout is None else timeout)
            try:
                self.frame.send(request)
                response = self.frame.receive()
            finally:
                self.connection.settimeout(self.timeout)
            if (response.get("version") != 1 or response.get("id") != request["id"]
                    or not isinstance(response.get("result"), dict)
                    or type(response["result"].get("ok")) is not bool):
                raise ConnectionError("Mismatched device response")
            if self.device:
                self.device.last_seen = datetime.now(timezone.utc).isoformat()
            return response["result"]

    def _heartbeat(self):
        while not self._stop.wait(self.heartbeat_interval):
            try:
                if not self._request({"operation": "heartbeat"}).get("ok"):
                    raise ConnectionError("Heartbeat rejected")
            except (OSError, ValueError, KeyError, sqlite3.Error):
                self.close()
                return

    def refresh(self):
        try:
            if not self.grants.get(self.fingerprint):
                self.close()
        except (OSError, ValueError, sqlite3.Error):
            self.close()
        return bool(self.device and self.device.online and not self._stop.is_set())

    def execute(self, capability, arguments, *, confirmed=False, request_id=None):
        try:
            grant = self.grants.get(self.fingerprint)
            if not grant or capability not in grant["capabilities"] or capability not in self.device.capabilities:
                return {"ok": False, "error": "Capability not granted or device revoked"}
            timeout = 30 if capability == "shell.powershell" else self.timeout
            return self._request({"operation": "execute", "id": request_id or str(uuid.uuid4()),
                                  "device_id": self.device.device_id, "capability": capability,
                                  "arguments": arguments, "confirmed": confirmed,
                                  "deadline": time.time() + min(timeout, 30)}, timeout=timeout)
        except (OSError, ValueError, KeyError, sqlite3.Error):
            self.close()
            return {"ok": False, "outcome": "unknown", "error": "Paired device connection lost; do not retry automatically"}

    def close(self):
        if self._stop.is_set():
            return
        self._stop.set()
        if self.device:
            self.device.online = False
        self.frame.close()
        thread = self._heartbeat_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=1)


class PairedLocalAgent:
    """Owns one loopback mTLS session and its outgoing client process for local validation."""
    def __init__(self, registry, profile, *, timeout=5.0, heartbeat_interval=5.0):
        self.profile = Path(profile).resolve()
        context = server_context(self.profile / "authority")
        grants = GrantStore(self.profile / "authority" / "grants.db")
        self.process = None
        self.transport = None
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            # Deliberately no public-bind parameter in the local MVP.
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(10)
            self.port = listener.getsockname()[1]
            entry = Path(__file__).with_name("paired_worker.py")
            self.process = subprocess.Popen(
                [sys.executable, "-I", "-X", "utf8", str(entry), "--profile", str(self.profile / "device"), "--port", str(self.port)],
                env=worker_environment(), cwd=str(entry.parent),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            raw, _ = listener.accept()
            raw.settimeout(timeout)
            try:
                connection = context.wrap_socket(raw, server_side=True)
            except Exception:
                raw.close()
                raise
            self.transport = PairedTransport(connection, grants, registry, timeout=timeout, heartbeat_interval=heartbeat_interval)
            self.device = self.transport.device
        except Exception:
            self.close()
            raise
        finally:
            listener.close()

    def execute(self, capability, arguments, **kwargs):
        return self.transport.execute(capability, arguments, **kwargs)

    def refresh(self):
        if self.process.poll() is not None:
            self.transport.close()
        return self.transport.refresh()

    def close(self):
        if self.transport:
            self.transport.close()
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
