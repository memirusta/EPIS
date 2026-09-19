"""Text-first EPIS 0.1 entry point; voice and UI intentionally remain later milestones."""

from __future__ import annotations

import os

from context_builder import ContextBuilder
from epis_core import build_system_prompt
from memory_manager import MemoryManager
from privacy import PrivacyFilter

from .core import AgentCore
from .devices import DeviceRegistry, LocalDeviceAgent
from .luna import OpenAILunaClient, OpenAISolClient
from .permissions import PermissionEngine
from .tools import build_local_registry


def main() -> int:
    memory = MemoryManager()
    registry = build_local_registry()
    state_path = os.path.join(memory.memory_dir, "devices.json")
    devices = DeviceRegistry(state_path)
    # The local agent executes only registry-owned, capability-scoped handlers.
    local_agent = LocalDeviceAgent(devices, lambda capability, args: _dispatch_capability(registry, capability, args))
    core = AgentCore(
        luna=OpenAILunaClient(),
        system_prompt=build_system_prompt(),
        context_builder=ContextBuilder(memory),
        memory=memory,
        registry=registry,
        devices=devices,
        local_agent=local_agent,
        permissions=PermissionEngine(),
        sol=OpenAISolClient(PrivacyFilter()),
    )
    print("EPIS 0.1 — text agent (Luna + deterministic Core)")
    print("Çıkış: quit | Onay beklerken: evet / hayır")
    while True:
        try:
            text = input("Sen: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nEPIS: Görüşürüz.")
            return 0
        if not text:
            continue
        if text.lower() in {"quit", "exit", "çık", "cik", "çıkış", "cikis"}:
            print("EPIS: Görüşürüz.")
            return 0
        try:
            if core.pending and text.lower() in {"evet", "onay", "yes"}:
                turn = core.confirm_pending()
            elif core.pending and text.lower() in {"hayır", "hayir", "iptal", "no"}:
                turn = core.reject_pending()
            else:
                turn = core.handle(text)
            print(f"EPIS: {turn.message}\n")
        except Exception as exc:
            print(f"EPIS: Bağlantı veya model hatası oluştu: {exc}\n")


def _dispatch_capability(registry, capability: str, arguments: dict) -> dict:
    for name in ("open_app", "close_app", "set_volume", "media_play_pause", "get_system_info"):
        entry = registry.get(name)
        if entry and entry[0].capability == capability:
            return registry.dispatch(name, arguments)
    return {"ok": False, "error": f"No local implementation for {capability}"}
