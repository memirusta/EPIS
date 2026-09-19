"""The EPIS 0.1 text-agent loop and deterministic execution boundary."""

from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
import json
import logging
import os
import time
from typing import Any

from .devices import DeviceRegistry
from .luna import LunaClient, ToolCall
from .permissions import PermissionEngine
from .tools import ToolRegistry
from .tasks import TaskStore
from .transport import DeviceTransport

logger = logging.getLogger("EPIS.AGENT")


@dataclass
class PendingAction:
    tool_call: ToolCall
    messages: list[dict]
    user_message: str
    remaining_calls: list[ToolCall] = field(default_factory=list)
    results: list[dict] = field(default_factory=list)
    model_steps: int = 0
    sol_delegations: int = 0
    expires_at: float = field(default_factory=lambda: time.monotonic() + 120)


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


SOL_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "delegate_to_sol",
        "description": (
            "Delegate only complex analysis, coding, planning, repository review, "
            "or long-chain reasoning to Sol. Do not use for normal conversation or local device tools."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Self-contained task for Sol, with only necessary context."},
                "reason": {"type": "string", "enum": ["analysis", "coding", "planning", "repository_review"]},
            },
            "required": ["task", "reason"],
            "additionalProperties": False,
        },
    },
}

CORE_TOOL_SCHEMAS = [
    {"type": "function", "function": {"name": name, "description": description,
     "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}}
    for name, description in (
        ("get_devices", "List registered devices, exact device IDs, current availability and capabilities. Use IDs from this result, never invent device IDs."),
        ("get_task_status", "Read recent EPIS device-command receipt states. Unknown means an action may have run; never retry it automatically."),
    )
]


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
        local_agent: DeviceTransport,
        permissions: PermissionEngine | None = None,
        sol: SolDelegator | None = None,
        tasks: TaskStore | None = None,
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
        self.tasks = tasks if tasks is not None else TaskStore()
        self.transports: dict[str, DeviceTransport] = {local_agent.device.device_id: local_agent}
        self._call_tasks: dict[str, str] = {}

    def handle(self, user_message: str) -> AgentTurn:
        if self.pending:
            # A new request must never inherit approval for an older action.
            self.reject_pending()
        self._call_tasks = {}
        mode = os.getenv("EPIS_LUNA_CONTEXT_MODE", "minimal").lower()
        if mode not in {"minimal", "local"}:
            raise ValueError("EPIS_LUNA_CONTEXT_MODE must be minimal or local")
        if mode == "minimal":
            context = self.context_builder.build_minimal()
        else:
            context = self.context_builder.build(user_message)
        system = self.system_prompt + "\n\n# AGENT SINIRI\n" + (
            "Tool sonucu görmeden işlem yapılmış gibi konuşma. Tool çağrıları yalnızca "
            "öneridir; Core izin ve cihaz kontrolünden geçirir. Tool sonucu geldikten sonra "
            "kullanıcıya doğal, kısa ve tek EPIS sesiyle yanıt ver. Karmaşık analiz, coding, "
            "planning veya repository inceleme gerekiyorsa delegate_to_sol kullan; normal "
            "sohbet ve yerel cihaz araçları için Sol'u çağırma. Sol sonucunu doğrudan yapıştırma, "
            "EPIS'in tutarlı sesiyle sentezle."
        )
        if context:
            system += "\n\n# ANLIK BAGLAM\n" + context
        messages = [{"role": "system", "content": system}, *self.history, {"role": "user", "content": user_message}]
        return self._run(messages, user_message)

    def confirm_pending(self) -> AgentTurn:
        if not self.pending:
            return AgentTurn("Onay bekleyen bir işlem yok.", [])
        pending, self.pending = self.pending, None
        if time.monotonic() > pending.expires_at:
            self.pending = pending
            turn = self.reject_pending()
            turn.message = "Onayın süresi doldu; işlem yapılmadı. İstersen yeniden iste."
            return turn
        turn = self._dispatch(pending.tool_call, confirmed=True)
        pending.results.extend(turn.tool_results)
        self._append_result(pending.messages, pending.tool_call, turn.tool_results[0])
        return self._run(pending.messages, pending.user_message, pending.results,
                         pending.model_steps, pending.sol_delegations, pending.remaining_calls)

    def reject_pending(self) -> AgentTurn:
        if not self.pending:
            return AgentTurn("Onay bekleyen bir işlem yok.", [])
        pending, self.pending = self.pending, None
        for call in [pending.tool_call, *pending.remaining_calls]:
            if call.call_id in self._call_tasks:
                self.tasks.finish(self._call_tasks[call.call_id], "cancelled")
            self._append_result(pending.messages, call, {"ok": False, "error": "Cancelled by user; not executed"})
        text = "Tamam, bekleyen işlemleri iptal ettim."
        pending.messages.append({"role": "assistant", "content": text})
        self._remember(pending.messages, pending.user_message, text)
        return AgentTurn(text, pending.results)

    def delegate_to_sol(self, task: str, context: dict | None = None) -> dict:
        """For a future Luna policy/routing decision; never called per local tool."""
        return self.sol.analyze(task, context)

    @staticmethod
    def _append_result(messages, call, result):
        messages.append({"role": "tool", "tool_call_id": call.call_id,
                         "content": json.dumps(result, ensure_ascii=False)})

    def _run(self, messages: list[dict], user_message: str, results=None,
             model_steps=0, sol_delegations=0, remaining_calls=None) -> AgentTurn:
        results = results if results is not None else []
        calls = list(remaining_calls or [])
        while calls or model_steps < 4:
            if not calls:
                try:
                    reply = self.luna.complete(messages, self._model_tools())
                except Exception as exc:
                    self._log("model_failed", error=type(exc).__name__)
                    text = ("Araç sonuçları alındı ama yanıt bağlantısı kesildi. İşlemleri otomatik tekrarlamadım."
                            if results else "Model bağlantısı kurulamadı. Anahtar, bakiye ve bağlantıyı kontrol edebilirsin.")
                    messages.append({"role": "assistant", "content": text})
                    self._remember(messages, user_message, text)
                    return AgentTurn(text, results)
                model_steps += 1
                messages.append(reply.as_assistant_message())
                if not reply.tool_calls:
                    text = reply.text or "Yanıt tamamlanamadı; tekrar dener misin?"
                    self._remember(messages, user_message, text)
                    return AgentTurn(text, results)
                calls = list(reply.tool_calls)
            batch, calls = calls, []
            for index, call in enumerate(batch):
                if any(result.get("outcome") == "unknown" for result in results):
                    result = {"ok": False, "error": "Previous execution outcome unknown; no further actions this turn. Ask user before any retry."}
                    results.append(result)
                    self._append_result(messages, call, result)
                    continue
                if model_steps >= 4 or len(results) >= 8:
                    result = {"ok": False, "error": "Tool budget exhausted; not executed"}
                    results.append(result)
                    self._append_result(messages, call, result)
                    continue
                if call.name == "delegate_to_sol":
                    error = self.registry._validate(SOL_TOOL_SCHEMA["function"]["parameters"], call.arguments)
                    if error:
                        result = {"ok": False, "error": error}
                    elif sol_delegations >= 1:
                        result = {"ok": False, "error": "Sol delegation limit reached for this turn"}
                    else:
                        sol_delegations += 1
                        task = call.arguments.get("task", "")
                        if not isinstance(task, str) or not task.strip():
                            result = {"ok": False, "error": "Sol task must be non-empty"}
                        else:
                            try:
                                result = self.delegate_to_sol(task.strip(), {"reason": call.arguments.get("reason")})
                            except Exception as exc:
                                result = {"ok": False, "error": type(exc).__name__}
                    self._log("sol_delegated", ok=result.get("ok"))
                    results.append(result)
                    self._append_result(messages, call, result)
                    continue
                turn = self._dispatch(call, confirmed=False)
                results.extend(turn.tool_results)
                if turn.confirmation_required:
                    self.pending = PendingAction(deepcopy(call), list(messages), user_message,
                                                 batch[index + 1:], results, model_steps, sol_delegations)
                    return AgentTurn(turn.message, results, True)
                for result in turn.tool_results:
                    self._append_result(messages, call, result)
        fallback = "İşlemi tamamlayamadım; fazla sayıda araç adımı oluştu."
        messages.append({"role": "assistant", "content": fallback})
        self._remember(messages, user_message, fallback)
        return AgentTurn(fallback, results)

    def _dispatch(self, call: ToolCall, confirmed: bool) -> AgentTurn:
        if call.name in {"get_devices", "get_task_status"}:
            error = self.registry._validate({"properties": {}, "additionalProperties": False}, call.arguments)
            if error:
                return AgentTurn("Geçersiz istek.", [{"ok": False, "error": error}])
            if call.name == "get_devices":
                for transport in self.transports.values():
                    transport.refresh()
                result = {"ok": True, "devices": self.devices.list_public()}
            else:
                result = {"ok": True, "tasks": self.tasks.recent()}
            return AgentTurn("", [result])
        entry = self.registry.get(call.name)
        if not entry:
            result = {"ok": False, "error": f"Unknown tool: {call.name}"}
            return AgentTurn("Bu işlemi desteklemiyorum.", [result])
        spec, _ = entry
        error = self.registry._validate(spec.schema, call.arguments)
        if error:
            return AgentTurn("Geçersiz araç isteği.", [{"ok": False, "error": error}])
        decision = self.permissions.decide(spec, call.arguments)
        if not decision.allowed:
            return AgentTurn("Bu işlem izin politikası tarafından engellendi.", [{"ok": False, "error": decision.reason}])
        target_device = call.arguments.get("device_id") or self.local_agent.device.device_id
        if call.call_id not in self._call_tasks:
            self._call_tasks[call.call_id] = self.tasks.create(call.name, target_device)
        task_id = self._call_tasks[call.call_id]
        if decision.requires_confirmation and not confirmed:
            detail = json.dumps(call.arguments, ensure_ascii=False)
            target = call.arguments.get("device_id") or self.local_agent.device.display_name
            return AgentTurn(f"{target}: {call.name} {detail}. Onaylıyor musun? (evet/hayır)", [], True)
        # A user/model may name a registered target. Without one, local is the
        # safe deterministic default rather than an arbitrary stale registry row.
        transport = self.transports.get(target_device)
        if transport:
            transport.refresh()
        device = self.devices.find_capable(spec.capability, preferred_device=target_device)
        if not device:
            self.tasks.finish(task_id, "failed")
            return AgentTurn("Bu işlemi yapabilecek çevrimiçi bir cihaz yok.", [{"ok": False, "error": f"No device for {spec.capability}"}])
        if not transport:
            self.tasks.finish(task_id, "failed")
            return AgentTurn("Uzak cihaz ajanı henüz bu oturumda bağlı değil.", [{"ok": False, "error": "remote agent unavailable"}])
        if not self.tasks.claim(task_id):
            return AgentTurn("Bu işlem tekrar çalıştırılmadı.", [{"ok": False, "task_id": task_id, "error": "Action already claimed; not replayed"}])
        try:
            result = transport.execute(spec.capability, call.arguments, confirmed=confirmed, request_id=task_id)
        except Exception as exc:
            result = {"ok": False, "outcome": "unknown", "error": type(exc).__name__}
        state = "unknown" if result.get("outcome") == "unknown" else "succeeded" if result.get("ok") else "failed"
        self.tasks.finish(task_id, state)
        self._log("tool_dispatched", tool=call.name, capability=spec.capability, device=device.device_id, ok=result.get("ok"))
        return AgentTurn("", [result])

    def _remember(self, messages: list[dict], user_message: str, response: str) -> None:
        history = [m for m in messages if m.get("role") != "system"]
        starts = [i for i, m in enumerate(history) if m.get("role") == "user"]
        # Retain whole turns so tool results never lose their call envelope.
        self.history = history[starts[max(0, len(starts) - 6)]:] if starts else []
        try:
            self.memory.log_interaction("agent_turn", f"kullanıcı: {user_message}\nEPIS: {response}", tags=["agentic-0.1"])
        except Exception as exc:
            self._log("memory_write_failed", error=type(exc).__name__)

    def _model_tools(self) -> list[dict]:
        return [*self.registry.openai_schemas(), *CORE_TOOL_SCHEMAS, SOL_TOOL_SCHEMA]

    def attach_transport(self, transport: DeviceTransport):
        """Trusted host wiring only. Never exposed to the model as a tool."""
        device_id = transport.device.device_id
        if device_id in self.transports:
            raise ValueError("Device already attached")
        self.devices.register(transport.device)
        self.transports[device_id] = transport

    def close(self):
        if self.pending:
            self.reject_pending()
        for transport in self.transports.values():
            transport.close()
        self.tasks.close()

    @staticmethod
    def _log(event: str, **data: Any) -> None:
        logger.info(json.dumps({"event": event, **data}, ensure_ascii=False, default=str))
