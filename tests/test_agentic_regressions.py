import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))
from agentic.devices import Device, DeviceRegistry
from agentic.luna import LunaReply, OpenAILunaClient, ToolCall, display_text
from agentic.permissions import PermissionEngine
from agentic.tools import ToolSpec, build_local_registry, _set_volume, _close_app
from context_builder import ContextBuilder
from epis_core import build_system_prompt, RESPONSE_PROTOCOL_BLOCK
from test_agentic_core import build_core, FakeSol


def assert_protocol(test, history):
    pending = set()
    for message in history:
        if message["role"] == "tool":
            test.assertIn(message["tool_call_id"], pending)
            pending.remove(message["tool_call_id"])
        else:
            test.assertFalse(pending)
            pending = {c["id"] for c in message.get("tool_calls", [])}
    test.assertFalse(pending)


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"EPIS_LUNA_CONTEXT_MODE": "minimal"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_minimal_does_not_access_any_private_provider(self):
        core = build_core([LunaReply(text="Hey!")])
        builder = ContextBuilder(Mock())
        with patch.object(builder, "build", side_effect=AssertionError("private read")), \
             patch.object(builder, "_get_gadgetbridge", side_effect=AssertionError("sensor read")):
            core.context_builder = builder
            self.assertEqual(core.handle("Hey!").message, "Hey!")
        self.assertEqual(builder.memory.mock_calls, [])

    def test_unknown_context_mode_does_not_opt_into_private_data(self):
        core = build_core([])
        with patch.dict(os.environ, {"EPIS_LUNA_CONTEXT_MODE": "minmal"}):
            with self.assertRaises(ValueError):
                core.handle("Hello")

    def test_identity_shared_but_legacy_protocol_and_private_seed_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            Path(folder, "epis_personality.md").write_text("shared identity", encoding="utf-8")
            Path(folder, "epis_personality_seed.md").write_text("private seed", encoding="utf-8")
            Path(folder, "identity_self.json").write_text('{"name":"private person"}', encoding="utf-8")
            with patch("epis_core.IDENTITY_DIR", folder):
                new = build_system_prompt(protocol="agentic", include_private=False)
                old = build_system_prompt()
        self.assertIn("shared identity", new)
        self.assertNotIn("private seed", new)
        self.assertNotIn("private person", new)
        self.assertNotIn(RESPONSE_PROTOCOL_BLOCK, new)
        self.assertIn(RESPONSE_PROTOCOL_BLOCK, old)
        self.assertIn("private person", old)

    def test_legacy_display_unwraps_only_direct_envelopes(self):
        self.assertEqual(display_text('{"type":"direct","message":"Hey!"}'), "Hey!")
        text = '{"type":"tool_call","payload":"shell"}'
        self.assertEqual(display_text(text), text)

    def test_explicit_unknown_or_offline_device_never_falls_back(self):
        registry = DeviceRegistry()
        registry.register(Device("local", "Local", "windows", {"system.info"}))
        registry.register(Device("offline", "Offline", "windows", {"system.info"}, online=False))
        self.assertIsNone(registry.find_capable("system.info", "missing"))
        self.assertIsNone(registry.find_capable("system.info", "offline"))

    def test_persisted_devices_require_new_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder, "devices.json"))
            registry = DeviceRegistry(path)
            registry.register(Device("remote", "Remote", "windows", {"codex.send"}))
            self.assertIsNone(DeviceRegistry(path).find_capable("codex.send", "remote"))

    def test_reserved_codex_capability_is_not_advertised_as_working(self):
        core = build_core([])
        self.assertNotIn("codex.send", core.local_agent.device.capabilities)

    def test_invalid_arguments_rejected_before_confirmation(self):
        for arguments in (None, [], {"untrusted": True}):
            core = build_core([LunaReply(tool_calls=[ToolCall("bad", "get_system_info", arguments)]), LunaReply(text="Rejected")], "yellow")
            turn = core.handle("test")
            self.assertFalse(turn.tool_results[0]["ok"])
            self.assertIsNone(core.pending)

    def test_unknown_risk_denied(self):
        self.assertFalse(PermissionEngine().decide(ToolSpec("bad", "", {}, "bad", "unknown"), {}).allowed)

    def test_red_requires_confirmation(self):
        core = build_core([LunaReply(tool_calls=[ToolCall("red", "get_system_info", {})]), LunaReply(text="Done")], "red")
        self.assertTrue(core.handle("test").confirmation_required)
        self.assertEqual(core.history, [])
        self.assertTrue(core.confirm_pending().tool_results[0]["ok"])

    def test_multi_tool_confirmation_each_approved_once(self):
        calls = [ToolCall("a", "get_system_info", {}), ToolCall("b", "get_system_info", {})]
        core = build_core([LunaReply(tool_calls=calls), LunaReply(text="Done")], "yellow")
        execute = Mock(wraps=core.local_agent.execute)
        core.local_agent.execute = execute
        self.assertTrue(core.handle("test").confirmation_required)
        self.assertEqual(execute.call_count, 0)
        self.assertTrue(core.confirm_pending().confirmation_required)
        self.assertEqual(execute.call_count, 1)
        turn = core.confirm_pending()
        self.assertFalse(turn.confirmation_required)
        self.assertEqual(execute.call_count, 2)
        assert_protocol(self, core.history)

    def test_rejection_cancels_batch_and_new_request_cannot_approve_old_action(self):
        calls = [ToolCall("a", "get_system_info", {}), ToolCall("b", "get_system_info", {})]
        core = build_core([LunaReply(tool_calls=calls), LunaReply(text="Hello")], "yellow")
        execute = Mock()
        core.local_agent.execute = execute
        core.handle("test")
        core.handle("new request")
        core.confirm_pending()
        execute.assert_not_called()
        assert_protocol(self, core.history)

    def test_confirmation_rechecks_device_availability(self):
        core = build_core([LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})]), LunaReply(text="Offline")], "yellow")
        core.handle("test")
        core.local_agent.device.online = False
        self.assertFalse(core.confirm_pending().tool_results[0]["ok"])

    def test_tool_budget_survives_confirmations(self):
        sol = FakeSol()
        core = build_core([
            LunaReply(tool_calls=[ToolCall("s1", "delegate_to_sol", {"task": "Analyze", "reason": "analysis"})]),
            LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})]),
            LunaReply(tool_calls=[ToolCall("s2", "delegate_to_sol", {"task": "Again", "reason": "analysis"})]),
            LunaReply(text="Done"),
        ], "yellow", sol)
        core.handle("test")
        turn = core.confirm_pending()
        self.assertEqual(len(sol.calls), 1)
        self.assertFalse(turn.tool_results[-1]["ok"])
        assert_protocol(self, core.history)

    def test_long_history_keeps_tool_pairs(self):
        replies = []
        for i in range(15):
            replies.extend([LunaReply(tool_calls=[ToolCall(str(i), "get_system_info", {})]), LunaReply(text="Done")])
        core = build_core(replies)
        for _ in range(15):
            core.handle("status")
            assert_protocol(self, core.history)
        self.assertEqual(sum(m["role"] == "user" for m in core.history), 6)

    def test_failure_after_execution_preserves_result_without_replay(self):
        core = build_core([])
        core.luna.complete = Mock(side_effect=[LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})]), RuntimeError("secret")])
        execute = Mock(wraps=core.local_agent.execute)
        core.local_agent.execute = execute
        turn = core.handle("status")
        self.assertTrue(turn.tool_results[0]["ok"])
        self.assertNotIn("secret", turn.message)
        self.assertEqual(execute.call_count, 1)
        assert_protocol(self, core.history)

    def test_adapter_invalid_json_is_not_empty_valid_call(self):
        client = OpenAILunaClient(api_key="test")
        sdk = Mock()
        msg = SimpleNamespace(content='', tool_calls=[SimpleNamespace(id="a", function=SimpleNamespace(name="media_play_pause", arguments="{"))])
        sdk.chat.completions.create.return_value = SimpleNamespace(choices=[SimpleNamespace(message=msg)])
        with patch.object(client, "_client", return_value=sdk):
            reply = client.complete([], build_local_registry().openai_schemas())
        self.assertIsNone(reply.tool_calls[0].arguments)
        result = build_local_registry().dispatch("media_play_pause", reply.tool_calls[0].arguments)
        self.assertFalse(result["ok"])

    def test_modern_volume_api_reads_back_actual_value(self):
        endpoint = Mock()
        endpoint.GetMasterVolumeLevelScalar.return_value = 0.2
        with patch("pycaw.pycaw.AudioUtilities.GetSpeakers", return_value=SimpleNamespace(EndpointVolume=endpoint)):
            result = _set_volume({"level": 20})
        endpoint.SetMasterVolumeLevelScalar.assert_called_once_with(0.2, None)
        self.assertEqual(result["level"], 20)
        self.assertTrue(result["ok"])

    def test_close_posts_window_message_and_never_terminates(self):
        process = Mock(info={"name": "notepad.exe", "pid": 123})
        with patch("psutil.process_iter", return_value=[process]), \
             patch("win32gui.EnumWindows", side_effect=lambda callback, value: callback(55, value)), \
             patch("win32gui.IsWindowVisible", return_value=True), \
             patch("win32process.GetWindowThreadProcessId", return_value=(0, 123)), \
             patch("win32gui.PostMessage") as post:
            result = _close_app({"app": "notepad"})
        process.terminate.assert_not_called()
        post.assert_called_once_with(55, 16, 0, 0)
        self.assertTrue(result["ok"])

    def test_volume_rejects_bounds_and_boolean_before_os(self):
        registry = build_local_registry()
        for value in (-1, 101, True, "20"):
            self.assertFalse(registry.dispatch("set_volume", {"level": value})["ok"])

    def test_loop_limit_does_not_leave_orphan_calls(self):
        replies = [LunaReply(tool_calls=[ToolCall(str(i), "get_system_info", {})]) for i in range(4)]
        core = build_core(replies)
        core.handle("status")
        assert_protocol(self, core.history)
        self.assertEqual(len(core.luna.calls), 4)


if __name__ == "__main__":
    unittest.main()
