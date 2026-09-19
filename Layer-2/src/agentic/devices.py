"""Capability-based device registration and local device-agent adapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
import socket
from typing import Callable


@dataclass
class Device:
    device_id: str
    display_name: str
    platform: str
    capabilities: set[str] = field(default_factory=set)
    online: bool = True
    last_seen: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    sensitive_state_local: bool = True

    def to_dict(self) -> dict:
        data = asdict(self)
        data["capabilities"] = sorted(self.capabilities)
        return data


class DeviceRegistry:
    """Local registry now; its API is suitable for a cloud sync backend later."""

    def __init__(self, state_path: str | None = None):
        self.state_path = state_path
        self._devices: dict[str, Device] = {}
        if state_path:
            self._load()

    def register(self, device: Device) -> Device:
        device.last_seen = datetime.now(timezone.utc).isoformat()
        self._devices[device.device_id] = device
        self._save()
        return device

    def get(self, device_id: str) -> Device | None:
        return self._devices.get(device_id)

    def find_capable(self, capability: str, preferred_device: str | None = None) -> Device | None:
        if preferred_device:
            candidate = self.get(preferred_device)
            if candidate and candidate.online and capability in candidate.capabilities:
                return candidate
            return None
        return next(
            (d for d in self._devices.values() if d.online and capability in d.capabilities),
            None,
        )

    def list_public(self) -> list[dict]:
        return [d.to_dict() for d in self._devices.values()]

    def _load(self) -> None:
        if not self.state_path or not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as handle:
                for raw in json.load(handle).get("devices", []):
                    raw["capabilities"] = set(raw.get("capabilities", []))
                    # Disk cache is not evidence of a live connection.
                    raw["online"] = False
                    self._devices[raw["device_id"]] = Device(**raw)
        except (OSError, ValueError, TypeError):
            # A corrupt availability cache must never prevent a local agent from starting.
            self._devices = {}

    def _save(self) -> None:
        if not self.state_path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.state_path)), exist_ok=True)
        payload = {"devices": self.list_public()}
        with open(self.state_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)


class LocalDeviceAgent:
    """Windows MVP adapter. A remote agent will implement this same contract."""

    def __init__(self, registry: DeviceRegistry, dispatcher: Callable[[str, dict], dict]):
        self.registry = registry
        self.dispatcher = dispatcher
        self.device = registry.register(Device(
            device_id=os.getenv("EPIS_DEVICE_ID", socket.gethostname().lower()),
            display_name=os.getenv("EPIS_DEVICE_NAME", socket.gethostname()),
            platform="windows" if os.name == "nt" else os.name,
            capabilities={
                "system.info", "app.open", "app.close", "audio.volume", "media.play_pause",
            },
        ))

    def execute(self, capability: str, arguments: dict) -> dict:
        if capability not in self.device.capabilities:
            return {"ok": False, "error": f"Capability unavailable: {capability}"}
        return self.dispatcher(capability, arguments)
