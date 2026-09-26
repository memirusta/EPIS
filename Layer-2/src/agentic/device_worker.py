"""Private stdio device worker. No model clients, memory retrieval or network listener."""
import json
import hashlib
from pathlib import Path
import platform
import re
import sys
import time

# Direct isolated Python entrypoint; never read PYTHONPATH or an env file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agentic.devices import DeviceRegistry, LocalDeviceAgent
from agentic.permissions import PermissionEngine
from agentic.tools import build_local_registry
from agentic.whatsapp_outreach import (
    WHATSAPP_DEVICE_CAPABILITIES,
    whatsapp_auto_stop_runtime_available,
)

PROTOCOL_VERSION = 1
MAX_FRAME = 2 * 1024 * 1024


class DeviceWorker:
    def __init__(self, registry=None, receipt_store=None, allowed_capabilities=None):
        using_default_registry = (
            registry is None
        )
        self.registry = registry or build_local_registry()
        self.local = LocalDeviceAgent(DeviceRegistry(), self.registry.dispatch_capability,
                                      self.registry.capabilities())
        self.permissions = PermissionEngine()
        self.receipts = receipt_store if receipt_store is not None else {}

        # Phase 1D registers the hidden protocol contracts on both
        # cloud and device registries, but production must not
        # advertise them until private bridge credentials have configured
        # a local controller. Connection may come up after this long-lived
        # worker starts; each send checks live bridge health again.
        if (
            using_default_registry
            and not whatsapp_auto_stop_runtime_available()
        ):
            self.local.device.capabilities.difference_update(
                WHATSAPP_DEVICE_CAPABILITIES
            )

        if allowed_capabilities is not None:
            self.local.device.capabilities.intersection_update(allowed_capabilities)

    def handle(self, message):
        if not isinstance(message, dict) or message.get("version") != PROTOCOL_VERSION:
            return {"ok": False, "error": "Unsupported protocol"}
        if message.get("operation") == "describe":
            return {"ok": True, "device": self.local.device.to_dict()}
        if message.get("operation") == "heartbeat":
            return {"ok": True}
        if message.get("operation") != "execute":
            return {"ok": False, "error": "Unsupported operation"}
        allowed = {"version", "operation", "id", "device_id", "capability", "arguments", "confirmed", "deadline"}
        if set(message) != allowed:
            return {"ok": False, "error": "Invalid command envelope"}
        if type(message["confirmed"]) is not bool:
            return {"ok": False, "error": "Invalid approval value"}
        request_id = message.get("id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{1,64}", request_id):
            return {"ok": False, "error": "Invalid command id"}
        fingerprint = hashlib.sha256(json.dumps(
            {k: v for k, v in message.items() if k != "deadline"}, sort_keys=True).encode()).hexdigest()
        deadline = message.get("deadline")
        if type(deadline) not in (int, float) or not time.time() <= deadline <= time.time() + 60:
            return {"ok": False, "error": "Expired or invalid command deadline"}
        if message.get("device_id") != self.local.device.device_id:
            return {"ok": False, "error": "Wrong target device"}
        capability = message.get("capability")
        if not isinstance(capability, str):
            return {"ok": False, "error": "Invalid capability"}
        if capability not in self.local.device.capabilities:
            return {"ok": False, "error": "Capability not granted to this device"}
        entry = self.registry.for_capability(capability)
        if not entry or platform.system().lower() not in entry[0].platforms:
            return {"ok": False, "error": "Capability unavailable"}
        spec, _ = entry
        error = self.registry._validate(spec.schema, message.get("arguments"))
        if error:
            return {"ok": False, "error": error}
        requested_device = message["arguments"].get("device_id")
        if requested_device is not None and requested_device != self.local.device.device_id:
            return {"ok": False, "error": "Conflicting target device"}
        decision = self.permissions.decide(spec, message["arguments"])
        if not decision.allowed or (decision.requires_confirmation and message["confirmed"] is not True):
            return {"ok": False, "error": "Device policy requires Core approval"}
        # Reauthorize even cached results after re-enrollment with narrower scope.
        if request_id in self.receipts:
            previous, result = self.receipts[request_id]
            if previous != fingerprint:
                return {"ok": False, "error": "Command id reused with different payload"}
            return result
        if len(self.receipts) >= 4096:
            return {"ok": False, "error": "Device receipt limit reached; explicit maintenance required"}
        # Reserve before the OS boundary. No replay if the handler fails ambiguously.
        unknown = {"ok": False, "outcome": "unknown", "error": "Execution outcome unknown; do not retry automatically"}
        if hasattr(self.receipts, "reserve"):
            if not self.receipts.reserve(request_id, fingerprint, unknown):
                previous, result = self.receipts[request_id]
                return result if previous == fingerprint else {"ok": False, "error": "Command id reused with different payload"}
        else:
            self.receipts[request_id] = (fingerprint, unknown)
        result = self.registry.dispatch(spec.name, message["arguments"])
        self.receipts[request_id] = (fingerprint, result)
        return result


def main():
    worker = DeviceWorker()
    while True:
        line = sys.stdin.buffer.readline(MAX_FRAME + 1)
        if not line:
            return 0
        if len(line) > MAX_FRAME or not line.endswith(b"\n"):
            return 2
        request_id = None
        try:
            message = json.loads(line)
            request_id = message.get("id") if isinstance(message, dict) else None
            result = worker.handle(message)
        except Exception:
            result = {"ok": False, "outcome": "unknown", "error": "Device command failed; outcome unknown"}
        frame = json.dumps({"version": PROTOCOL_VERSION, "id": request_id, "result": result}, ensure_ascii=False)
        sys.stdout.buffer.write((frame + "\n").encode("utf-8"))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(main())
