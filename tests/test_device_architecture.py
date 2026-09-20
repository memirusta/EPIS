import json
import os
from pathlib import Path
import platform
import queue
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"))
from agentic.devices import Device, DeviceRegistry, LocalDeviceAgent
from agentic.device_worker import DeviceWorker
from agentic.luna import LunaReply, ToolCall
from agentic.tasks import TaskStore
from agentic.tools import ToolRegistry, ToolSpec, build_local_registry, _open_url
from agentic.transport import StdioDeviceAgent, worker_environment
from test_agentic_core import build_core


class DeviceArchitectureTests(unittest.TestCase):
    def worker(self, risk="green"):
        registry = ToolRegistry()
        handler = Mock(return_value={"ok": True})
        registry.register(ToolSpec("test", "", {"type": "object", "properties": {}, "additionalProperties": False},
                                   "test.capability", risk, platforms=(platform.system().lower(),)), handler)
        worker = DeviceWorker(registry)
        return worker, handler

    def command(self, worker, **changes):
        command = {"version": 1, "operation": "execute", "id": "test-1",
                   "device_id": worker.local.device.device_id, "capability": "test.capability",
                   "arguments": {}, "confirmed": False, "deadline": time.time() + 20}
        command.update(changes)
        return command

    def test_worker_checks_approval_locally(self):
        for risk in ("yellow", "red"):
            worker, handler = self.worker(risk)
            for confirmed in (False, "true", 1, None):
                self.assertFalse(worker.handle(self.command(worker, confirmed=confirmed))["ok"])
            handler.assert_not_called()
            self.assertTrue(worker.handle(self.command(worker, confirmed=True))["ok"])
            handler.assert_called_once()

    def test_worker_deduplicates_exact_request(self):
        worker, handler = self.worker()
        command = self.command(worker)
        self.assertTrue(worker.handle(command)["ok"])
        self.assertTrue(worker.handle(command)["ok"])
        handler.assert_called_once()
        self.assertFalse(worker.handle({**command, "confirmed": True})["ok"])

    def test_worker_rejects_wrong_target_expiry_schema_and_protocol(self):
        worker, handler = self.worker()
        for changes in ({"device_id": "other"}, {"deadline": time.time()-1},
                        {"deadline": float("nan")}, {"deadline": "later"},
                        {"arguments": {"shell": "oops"}}, {"version": 2},
                        {"capability": "shell"}, {"capability": []}, {"arguments": []},
                        {"id": "../../file"}, {"extra": "ignored?"}):
            self.assertFalse(worker.handle(self.command(worker, **changes))["ok"], changes)
        handler.assert_not_called()

    def test_worker_receipt_limit_fails_closed_without_eviction(self):
        worker, handler = self.worker()
        worker.receipts = {str(i): ("", {}) for i in range(4096)}
        self.assertFalse(worker.handle(self.command(worker))["ok"])
        handler.assert_not_called()

    def test_manifest_matches_platform_registry_and_blank_name_defaults(self):
        with patch.dict(os.environ, {"EPIS_DEVICE_ID": "", "EPIS_DEVICE_NAME": ""}):
            worker = DeviceWorker()
        manifest = worker.handle({"version": 1, "operation": "describe"})["device"]
        self.assertTrue(manifest["device_id"])
        self.assertTrue(manifest["display_name"])
        self.assertEqual(set(manifest["capabilities"]), build_local_registry().capabilities())
        self.assertNotIn("codex.send", manifest["capabilities"])

    def test_child_environment_excludes_keys_and_python_injection(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "secret", "LUNA_API_KEY": "secret",
                                    "PYTHONPATH": "bad", "PYTHONSTARTUP": "bad", "EPIS_DEVICE_NAME": "Legion"}):
            env = worker_environment()
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("LUNA_API_KEY", env)
        self.assertNotIn("PYTHONPATH", env)
        self.assertNotIn("PYTHONSTARTUP", env)
        self.assertEqual(env["EPIS_DEVICE_NAME"], "Legion")

    def test_task_journal_restart_does_not_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder, "tasks.db"))
            store = TaskStore(path)
            running = store.create("set_volume", "legion")
            waiting = store.create("close_app", "legion")
            done = store.create("get_system_info", "legion")
            self.assertTrue(store.claim(running))
            self.assertFalse(store.claim(running))
            store.claim(done)
            store.finish(done, "succeeded")
            store.close()
            store = TaskStore(path)
            try:
                rows = {row["task_id"]: row for row in store.recent()}
                self.assertEqual(rows[running]["state"], "unknown")
                self.assertEqual(rows[waiting]["state"], "cancelled")
                self.assertEqual(rows[done]["state"], "succeeded")
                self.assertFalse(store.claim(running))
                self.assertNotIn("arguments", rows[done])
                self.assertNotIn("result", rows[done])
            finally:
                store.close()

    def test_journal_has_single_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = str(Path(folder, "tasks.db"))
            store = TaskStore(path)
            try:
                with self.assertRaises(RuntimeError):
                    TaskStore(path)
            finally:
                store.close()

    def test_confirmation_expiry_cancels_receipt(self):
        core = build_core([LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})])], "yellow")
        self.addCleanup(core.close)
        execute = Mock(wraps=core.local_agent.execute)
        core.local_agent.execute = execute
        core.handle("test")
        core.pending.expires_at = 0
        self.assertIn("süresi doldu", core.confirm_pending().message)
        execute.assert_not_called()
        self.assertEqual(core.tasks.recent()[0]["state"], "cancelled")

    def test_unknown_outcome_recorded_without_automatic_retry(self):
        core = build_core([LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})]), LunaReply(text="Unknown")])
        self.addCleanup(core.close)
        core.local_agent.execute = Mock(return_value={"ok": False, "outcome": "unknown"})
        core.handle("test")
        self.assertEqual(core.tasks.recent()[0]["state"], "unknown")
        core.local_agent.execute.assert_called_once()

    def test_unknown_outcome_blocks_model_retry_even_with_new_call_id(self):
        core = build_core([
            LunaReply(tool_calls=[ToolCall("a", "get_system_info", {})]),
            LunaReply(tool_calls=[ToolCall("retry", "get_system_info", {})]),
            LunaReply(text="Outcome unknown; not retried"),
        ])
        self.addCleanup(core.close)
        core.local_agent.execute = Mock(return_value={"ok": False, "outcome": "unknown"})
        turn = core.handle("test")
        core.local_agent.execute.assert_called_once()
        self.assertIn("no further actions", turn.tool_results[1]["error"])

    def test_second_trusted_transport_routes_by_exact_device(self):
        core = build_core([])
        self.addCleanup(core.close)
        registry = ToolRegistry()
        spec = ToolSpec("get_system_info", "", {"properties": {"device_id": {"type": "string"}}, "additionalProperties": False}, "system.info")
        registry.register(spec, Mock())
        core.registry = registry
        with patch.dict(os.environ, {"EPIS_DEVICE_ID": "second"}):
            second = LocalDeviceAgent(DeviceRegistry(), Mock(return_value={"ok": True, "from": "second"}))
        core.attach_transport(second)
        result = core._dispatch(ToolCall("a", "get_system_info", {"device_id": "second"}), False)
        self.assertEqual(result.tool_results[0]["from"], "second")
        second.dispatcher.assert_called_once()

    def test_core_device_and_task_tools_are_read_only(self):
        core = build_core([])
        self.addCleanup(core.close)
        core.local_agent.execute = Mock()
        result = core._dispatch(ToolCall("d", "get_devices", {}), False)
        self.assertEqual(len(result.tool_results[0]["devices"]), 1)
        self.assertEqual(core._dispatch(ToolCall("t", "get_task_status", {}), False).tool_results[0]["tasks"], [])
        self.assertFalse(core._dispatch(ToolCall("bad", "get_devices", {"command": "run"}), False).tool_results[0]["ok"])
        core.local_agent.execute.assert_not_called()

    def test_url_handler_rejects_unsafe_schemes_and_authorities(self):
        with patch("os.startfile") as start:
            for url in ("file:///C:/secret", "javascript:alert(1)", "http://example.com", "https://user:pass@example.com",
                        "https://example.com:8080", "https://example.com\n", "https://example.com\\oops", "https://"):
                self.assertFalse(_open_url({"url": url})["ok"], url)
            start.assert_not_called()
            self.assertTrue(_open_url({"url": "https://example.com/path?q=test"})["ok"])
            start.assert_called_once_with("https://example.com/path?q=test")

    def test_new_registry_metadata(self):
        registry = build_local_registry()
        self.assertEqual(len(registry.specs()), 30)
        self.assertEqual(registry.get("open_url")[0].risk_class, "yellow")
        self.assertIn("system.battery", registry.capabilities("windows"))
        self.assertEqual(registry.capabilities("linux"), set())

    def test_battery_and_media_skip_os_adapters(self):
        from types import SimpleNamespace
        registry = build_local_registry()
        with patch("psutil.sensors_battery", return_value=SimpleNamespace(percent=42, power_plugged=True)):
            self.assertEqual(registry.dispatch("get_battery", {}),
                             {"ok": True, "present": True, "percent": 42, "plugged_in": True})
        with patch("psutil.sensors_battery", return_value=None):
            self.assertFalse(registry.dispatch("get_battery", {})["present"])
        with patch("win32api.keybd_event") as key:
            self.assertTrue(registry.dispatch("media_next", {})["ok"])
            self.assertTrue(registry.dispatch("media_previous", {})["ok"])
            self.assertEqual(key.call_count, 4)
            self.assertEqual(key.call_args_list[0].args[0], 0xB0)
            self.assertEqual(key.call_args_list[2].args[0], 0xB1)

    def test_explicit_unknown_transport_mode_fails_closed(self):
        from agentic.cli import create_core
        with patch.dict(os.environ, {"EPIS_DEVICE_TRANSPORT": "stdoi"}), \
             patch("agentic.cli.MemoryManager") as memory, \
             patch("agentic.cli.DeviceRegistry"), \
             patch("agentic.cli.StdioDeviceAgent") as transport:
            memory.return_value.memory_dir = "."
            with self.assertRaises(ValueError):
                create_core()
            transport.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows worker integration")
    def test_core_to_real_worker_and_back_without_external_model(self):
        core = build_core([LunaReply(tool_calls=[ToolCall("real", "get_system_info", {})]),
                           LunaReply(text="Sistem bilgisi cihaz ajanından geldi.")])
        core.local_agent.close()
        core.local_agent = StdioDeviceAgent(core.devices)
        core.transports = {core.local_agent.device.device_id: core.local_agent}
        self.addCleanup(core.close)
        turn = core.handle("Bilgisayarın durumu ne?")
        self.assertTrue(turn.tool_results[0]["ok"])
        self.assertIn("cpu_percent", turn.tool_results[0])
        self.assertEqual(core.tasks.recent()[0]["state"], "succeeded")
        tool_messages = [m for m in core.luna.calls[-1][0] if m["role"] == "tool"]
        self.assertEqual(json.loads(tool_messages[0]["content"]), turn.tool_results[0])

    @unittest.skipUnless(os.name == "nt", "Windows worker integration")
    def test_actual_process_handshake_readonly_roundtrip_and_cleanup(self):
        agent = StdioDeviceAgent(DeviceRegistry())
        try:
            self.assertNotEqual(agent.process.pid, os.getpid())
            self.assertTrue(agent.refresh())
            self.assertIn("system.battery", agent.device.capabilities)
            self.assertTrue(agent.execute("system.info", {}, request_id="real-status")["ok"])
            self.assertTrue(agent.execute("system.battery", {}, request_id="real-battery")["ok"])
            # Sensitive OS action must be rejected even if Core were bypassed.
            self.assertFalse(agent.execute("app.close", {"app": "notepad"}, request_id="unapproved-close")["ok"])
        finally:
            agent.close()
        self.assertIsNotNone(agent.process.poll())
        self.assertFalse(agent.refresh())

    @unittest.skipUnless(os.name == "nt", "Windows worker integration")
    def test_transport_timeout_marks_unknown_and_stops_owned_worker(self):
        agent = StdioDeviceAgent(DeviceRegistry())
        try:
            with patch.object(agent, "_request", side_effect=queue.Empty):
                result = agent.execute("system.info", {})
            self.assertEqual(result["outcome"], "unknown")
            self.assertFalse(agent.refresh())
            self.assertIsNotNone(agent.process.poll())
        finally:
            agent.close()


if __name__ == "__main__":
    unittest.main()
