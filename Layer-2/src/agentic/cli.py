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
from .transport import StdioDeviceAgent
from .tasks import TaskStore


def create_core() -> AgentCore:
    mode = os.getenv("EPIS_LUNA_CONTEXT_MODE", "minimal").lower()
    if mode not in {"minimal", "local"}:
        raise ValueError("Unknown context mode")
    memory = MemoryManager()
    registry = build_local_registry()
    state_path = os.path.join(memory.memory_dir, "devices.json")
    devices = DeviceRegistry(state_path)
    transport_mode = os.getenv("EPIS_DEVICE_TRANSPORT", "stdio").lower()
    if transport_mode not in {"stdio", "inprocess"}:
        raise ValueError("EPIS_DEVICE_TRANSPORT must be stdio or inprocess")
    tasks = TaskStore(os.path.join(memory.memory_dir, "agent_tasks.db"))
    local_agent = None
    try:
        # In-process remains explicit compatibility, never a silent fallback.
        local_agent = (StdioDeviceAgent(devices) if transport_mode == "stdio" else
                       LocalDeviceAgent(devices, registry.dispatch_capability, registry.capabilities()))
        return AgentCore(
            luna=OpenAILunaClient(),
            system_prompt=build_system_prompt(protocol="agentic", include_private=mode == "local"),
            context_builder=ContextBuilder(memory),
            memory=memory,
            registry=registry,
            devices=devices,
            local_agent=local_agent,
            permissions=PermissionEngine(),
            sol=OpenAISolClient(PrivacyFilter()),
            tasks=tasks,
        )
    except Exception:
        if local_agent:
            local_agent.close()
        tasks.close()
        raise
def main() -> int:
    if not (os.getenv("LUNA_API_KEY") or os.getenv("OPENAI_API_KEY")):
        print("EPIS: API anahtarı bulunamadı. --env-file ile keys.env dosyanı seç.")
        return 1
    core = create_core()
    try:
        return interact(core)
    finally:
        core.close()


def interact(core) -> int:
    print("EPIS 0.1 — Luna + Sol")
    print(f"Cihaz: {core.local_agent.device.display_name} | Bağlantı: {os.getenv('EPIS_DEVICE_TRANSPORT', 'stdio')}")
    print("Bu oturumda yazdıkların ve araç sonuçları model API'sine gönderilir.")
    if os.getenv("EPIS_LUNA_CONTEXT_MODE", "minimal").lower() == "minimal":
        print("Bağlam: minimal — eski hafıza ve sensörler okunmaz; yeni konuşma yerelde kaydedilir.")
    print("Çıkış: quit | Onay: evet / hayır (120 sn) | /devices /tasks /tools")
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
        if text.lower() in {"/devices", "/tasks", "/tools"}:
            if text.lower() == "/devices":
                for transport in core.transports.values():
                    transport.refresh()
                for device in core.devices.list_public():
                    print(f"{device['display_name']} ({device['device_id']}): {'online' if device['online'] else 'offline'} | {', '.join(device['capabilities'])}")
            elif text.lower() == "/tasks":
                rows = core.tasks.recent()
                for row in rows:
                    print(f"{row['task_id'][:8]} | {row['tool']} | {row['device']} | {row['state']}")
                if not rows:
                    print("Henüz cihaz işlemi yok.")
            else:
                for spec in core.registry.specs():
                    print(f"{spec.name} | {spec.capability} | {spec.risk_class}")
            continue
        try:
            if core.pending and text.lower() in {"evet", "onay", "yes"}:
                turn = core.confirm_pending()
            elif core.pending and text.lower() in {"hayır", "hayir", "iptal", "no"}:
                turn = core.reject_pending()
            else:
                turn = core.handle(text)
            print(f"EPIS: {turn.message}\n")
        except Exception as exc:
            import logging
            logging.getLogger("EPIS.AGENT").error("Turn failed: %s", type(exc).__name__)
            print("EPIS: Bu tur tamamlanamadı. Hata türü epis.log dosyasına kaydedildi.\n")


def _dispatch_capability(registry, capability: str, arguments: dict) -> dict:
    return registry.dispatch_capability(capability, arguments)
