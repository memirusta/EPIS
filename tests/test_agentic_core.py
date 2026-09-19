import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "Layer-2", "src"))

from agentic.core import AgentCore
from agentic.devices import DeviceRegistry, LocalDeviceAgent
from agentic.luna import LunaReply, OpenAILunaClient, ToolCall
from agentic.permissions import PermissionEngine, RiskClass
from agentic.tools import ToolRegistry, ToolSpec, build_local_registry


class FakeLuna:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, tools):
        self.calls.append((messages, tools))
        return self.replies.pop(0)


class FakeContext:
    def build(self, user_message):
        return "## ZAMAN\nTest zamanı"


class PrivateFakeContext:
    def build(self, user_message):
        return "## ZAMAN\nTest zamanı\n\n## İLGİLİ GEÇMİŞ\nprivate memory"


class FakeMemory:
    def __init__(self):
        self.rows = []

    def log_interaction(self, *args, **kwargs):
        self.rows.append((args, kwargs))


class FakeSol:
    def __init__(self):
        self.calls = []

    def analyze(self, task, context=None):
        self.calls.append((task, context))
        return {"ok": True, "model_used": "gpt-5.6-sol", "result": "Sol analysis"}


def build_core(replies, risk=RiskClass.GREEN.value, sol=None):
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            "get_system_info", "Read test status.",
            {"type": "object", "properties": {}, "additionalProperties": False},
            "system.info", risk, risk != RiskClass.GREEN.value,
        ),
        lambda args: {"ok": True, "hostname": "legion-test"},
    )
    devices = DeviceRegistry()
    local = LocalDeviceAgent(
        devices,
        lambda capability, args: registry.dispatch("get_system_info", args),
    )
    return AgentCore(
        luna=FakeLuna(replies), system_prompt="EPIS identity", context_builder=FakeContext(),
        memory=FakeMemory(), registry=registry, devices=devices, local_agent=local,
        permissions=PermissionEngine(), sol=sol,
    )


class AgentCoreTests(unittest.TestCase):
    def test_luna_defaults_to_no_reasoning_for_chat_tools(self):
        old = os.environ.pop("LUNA_REASONING_EFFORT", None)
        try:
            self.assertEqual(OpenAILunaClient(api_key="test-key").reasoning_effort, "none")
        finally:
            if old is not None:
                os.environ["LUNA_REASONING_EFFORT"] = old

    def test_green_tool_executes_and_luna_owns_final_voice(self):
        core = build_core([
            LunaReply(tool_calls=[ToolCall("call-1", "get_system_info", {})]),
            LunaReply(text="Legion şu anda erişilebilir görünüyor."),
        ])
        turn = core.handle("Bilgisayarın durumu ne?")
        self.assertFalse(turn.confirmation_required)
        self.assertEqual(turn.message, "Legion şu anda erişilebilir görünüyor.")
        self.assertEqual(turn.tool_results, [{"ok": True, "hostname": "legion-test"}])

    def test_yellow_tool_needs_explicit_confirmation(self):
        core = build_core([
            LunaReply(tool_calls=[ToolCall("call-2", "get_system_info", {})]),
            LunaReply(text="Onayla bilgisayar durumunu kontrol ettim."),
        ], RiskClass.YELLOW.value)
        turn = core.handle("Kapat")
        self.assertTrue(turn.confirmation_required)
        self.assertIsNotNone(core.pending)
        confirmed = core.confirm_pending()
        self.assertEqual(confirmed.message, "Onayla bilgisayar durumunu kontrol ettim.")
        self.assertEqual(core.history[-1]["role"], "assistant")

    def test_unknown_tool_is_not_dispatched(self):
        core = build_core([
            LunaReply(tool_calls=[ToolCall("call-3", "shell", {"command": "whoami"})]),
            LunaReply(text="Bu komutu çalıştıramam."),
        ])
        turn = core.handle("Komut çalıştır")
        self.assertEqual(turn.tool_results[0]["ok"], False)
        self.assertIn("Unknown tool", turn.tool_results[0]["error"])

    def test_registry_rejects_unexpected_model_arguments(self):
        registry = build_local_registry()
        result = registry.dispatch("get_system_info", {"command": "whoami"})
        self.assertFalse(result["ok"])
        self.assertIn("unexpected argument", result["error"])

    def test_minimal_context_mode_excludes_retrieved_memory(self):
        core = build_core([LunaReply(text="Tamam.")])
        core.context_builder = PrivateFakeContext()
        old = os.environ.get("EPIS_LUNA_CONTEXT_MODE")
        os.environ["EPIS_LUNA_CONTEXT_MODE"] = "minimal"
        try:
            core.handle("Nasılsın?")
            system = core.luna.calls[0][0][0]["content"]
            self.assertIn("Test zamanı", system)
            self.assertNotIn("private memory", system)
        finally:
            if old is None:
                os.environ.pop("EPIS_LUNA_CONTEXT_MODE", None)
            else:
                os.environ["EPIS_LUNA_CONTEXT_MODE"] = old

    def test_luna_delegates_complex_reasoning_to_sol_then_synthesizes(self):
        sol = FakeSol()
        core = build_core([
            LunaReply(tool_calls=[ToolCall(
                "call-sol", "delegate_to_sol",
                {"task": "Review this repository architecture", "reason": "repository_review"},
            )]),
            LunaReply(text="Mimari incelemenin sonucu bu."),
        ], sol=sol)
        turn = core.handle("Bu repository mimarisini derin incele")
        self.assertEqual(turn.message, "Mimari incelemenin sonucu bu.")
        self.assertEqual(turn.tool_results[0]["model_used"], "gpt-5.6-sol")
        self.assertEqual(sol.calls[0][1]["reason"], "repository_review")


if __name__ == "__main__":
    unittest.main()
