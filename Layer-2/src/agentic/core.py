"""The EPIS 0.1 text-agent loop and deterministic execution boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from typing import Any

from .devices import DeviceRegistry, LocalDeviceAgent
from .luna import LunaClient, LunaReply, ToolCall
from .permissions import PermissionEngine
from .tools import ToolRegistry

logger = logging.getLogger("EPIS.AGENT")


@dataclass
class PendingAction:
    tool_call: ToolCall
    messages: list[dict]
    user_message: str


@dataclass
class AgentTurn:
    message: str
    tool_results: list[dict]
    confirmation_required: bool = False


class SolDelegator:
    """Explicit heavy-reasoning boundary; not part of ordinary tool dispatch."""

    def __init__(self, router=None):
        self.router = router

    def analyze(self, task: str, context: dict | None = None) -> dict:
        if self.router is None:
            return {"ok": False, "error": "Sol is not configured"}
        # Existing router retains its privacy filter before a Layer-3 call.
        return self.router.intercept_tool_call(
            {"task_type": "deep_analysis", "payload": task}, context or {}
        )


class AgentCore:
    """Coordinates Luna, local context, device routing, and controlled tools.

    The model can suggest a call. Only this class selects a capable device,
    checks permissions, dispatches the constrained implementation, and feeds
    the observed result back to Luna for a single EPIS voice.
    """

    def __init__(
        self,
        luna: LunaClient,
        system_prompt: str,
        context_builder,
        memory,
        registry: ToolRegistry,
        devices: DeviceRegistry,
        local_agent: LocalDeviceAgent,
        permissions: PermissionEngine | None = None,
        sol: SolDelegator | None = None,
    ):
        self.luna = luna
        self.system_prompt = system_prompt
        self.context_builder = context_builder
        self.memory = memory
        self.registry = registry
        self.devices = devices
        self.local_agent = local_agent
        self.permissions = permissions or PermissionEngine()
        self.sol = sol or SolDelegator()
        self.history: list[dict] = []
        self.pending: PendingAction | None = None

    def handle(self, user_message: str) -> AgentTurn:
        context = self._context_for_luna(self.context_builder.build(user_message))
        system = self.system_prompt + "\n\n# AGENT SINIRI\n" + (
            "Tool sonucu görmeden işlem yapılmış gibi konuşma. Tool çağrıları yalnızca "
            "öneridir; Core izin ve cihaz kontrolünden geçirir. Tool sonucu geldikten sonra "
            "kullanıcıya doğal, kısa ve tek EPIS sesiyle yanıt ver."
        )
        if context:
            system += "\n\n# ANLIK BAGLAM\n" + context
        messages = [{"role": "system", "content": system}, *self.history, {"role": "user", "content": user_message}]
        return self._run(messages, user_message)

    def confirm_pending(self) -> AgentTurn:
        if not self.pending:
            return AgentTurn("Onay bekleyen bir işlem yok.", [])
        pending, self.pending = self.pending, None
        return self._dispatch_and_respond(pending.tool_call, pending.messages, pending.user_message, confirmed=True)

    def reject_pending(self) -> AgentTurn:
        if not self.pending:
            return AgentTurn("Onay bekleyen bir işlem yok.", [])
        self.pending = None
        return AgentTurn("Tamam, o işlemi yapmadım.", [])

    def delegate_to_sol(self, task: str, context: dict | None = None) -> dict:
        """For a future Luna policy/routing decision; never called per local tool."""
        return self.sol.analyze(task, context)

    def _run(self, messages: list[dict], user_message: str) -> AgentTurn:
        results: list[dict] = []
        for _ in range(3):
            reply = self.luna.complete(messages, self.registry.openai_schemas())
            messages.append(reply.as_assistant_message())
            if not reply.tool_calls:
                text = reply.text or "Yanıt tamamlanamadı; tekrar dener misin?"
                self._remember(messages, user_message, text)
                return AgentTurn(text, results)
            for call in reply.tool_calls:
                turn = self._dispatch_and_respond(call, messages, user_message, confirmed=False, defer_response=True)
                results.extend(turn.tool_results)
                if turn.confirmation_required:
                    return AgentTurn(turn.message, results, True)
                for result in turn.tool_results:
                    messages.append({"role": "tool", "tool_call_id": call.call_id, "content": json.dumps(result, ensure_ascii=False)})
        fallback = "İşlemi tamamlayamadım; fazla sayıda araç adımı oluştu."
        self._remember(messages, user_message, fallback)
        return AgentTurn(fallback, results)

    def _dispatch_and_respond(self, call: ToolCall, messages: list[dict], user_message: str, confirmed: bool, defer_response: bool = False) -> AgentTurn:
        entry = self.registry.get(call.name)
        if not entry:
            result = {"ok": False, "error": f"Unknown tool: {call.name}"}
            return AgentTurn("Bu işlemi desteklemiyorum.", [result])
        spec, _ = entry
        decision = self.permissions.decide(spec, call.arguments)
        if not decision.allowed:
            return AgentTurn("Bu işlem izin politikası tarafından engellendi.", [{"ok": False, "error": decision.reason}])
        if decision.requires_confirmation and not confirmed:
            self.pending = PendingAction(call, list(messages), user_message)
            return AgentTurn(f"{call.name} işlemi onay gerektiriyor. Devam etmemi ister misin?", [], True)
        # A user/model may name a registered target. Without one, local is the
        # safe deterministic default rather than an arbitrary stale registry row.
        target_device = call.arguments.get("device_id") or self.local_agent.device.device_id
        device = self.devices.find_capable(spec.capability, preferred_device=target_device)
        if not device:
            return AgentTurn("Bu işlemi yapabilecek çevrimiçi bir cihaz yok.", [{"ok": False, "error": f"No device for {spec.capability}"}])
        if device.device_id != self.local_agent.device.device_id:
            return AgentTurn("Uzak cihaz ajanı henüz bu oturumda bağlı değil.", [{"ok": False, "error": "remote agent unavailable"}])
        result = self.local_agent.execute(spec.capability, call.arguments)
        self._log("tool_dispatched", tool=call.name, capability=spec.capability, device=device.device_id, ok=result.get("ok"))
        if defer_response:
            return AgentTurn("", [result])
        messages = [*messages, {"role": "tool", "tool_call_id": call.call_id, "content": json.dumps(result, ensure_ascii=False)}]
        reply = self.luna.complete(messages, self.registry.openai_schemas())
        text = reply.text or ("İşlem tamamlandı." if result.get("ok") else "İşlem tamamlanamadı.")
        messages.append(reply.as_assistant_message())
        self._remember(messages, user_message, text)
        return AgentTurn(text, [result])

    def _remember(self, messages: list[dict], user_message: str, response: str) -> None:
        self.history = [m for m in messages if m.get("role") != "system"][-24:]
        try:
            self.memory.log_interaction("agent_turn", f"kullanıcı: {user_message}\nEPIS: {response}", tags=["agentic-0.1"])
        except Exception as exc:
            self._log("memory_write_failed", error=str(exc))

    @staticmethod
    def _context_for_luna(context: str) -> str:
        """Keep remote-frontline exposure deliberately small when requested.

        `local` is the MVP default because the documented Luna endpoint is
        localhost. Configure `EPIS_LUNA_CONTEXT_MODE=minimal` before using an
        untrusted/hosted frontline endpoint; privacy-aware Sol routing remains
        separate and continues to use PrivacyFilter.
        """
        if os.getenv("EPIS_LUNA_CONTEXT_MODE", "local").lower() != "minimal":
            return context
        start = context.find("## ZAMAN")
        if start < 0:
            return ""
        next_section = context.find("\n\n##", start + len("## ZAMAN"))
        return context[start:next_section if next_section >= 0 else len(context)]

    @staticmethod
    def _log(event: str, **data: Any) -> None:
        logger.info(json.dumps({"event": event, **data}, ensure_ascii=False, default=str))
