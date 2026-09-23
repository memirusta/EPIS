import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
os.sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from agentic.core import AgentCore
from agentic.devices import DeviceRegistry, LocalDeviceAgent
from agentic.hot_memory import HotConversationStore
from agentic.luna import LunaReply
from agentic.permissions import PermissionEngine
from agentic.runtime_memory import CloudRuntimeMemory
from agentic.tools import ToolRegistry, build_local_registry
from agentic.trusted_context import _personal_context
from server import app as server_app
from test_agentic_core import FakeContext, FakeMemory, FakeLuna


class Phase5RuntimeTests(unittest.TestCase):
    def test_cloud_runtime_memory_has_no_personal_memory(self):
        with tempfile.TemporaryDirectory() as folder:
            memory = CloudRuntimeMemory(folder)
            self.assertEqual(memory.get_current_state(), {})
            self.assertEqual(memory.get_recent_interactions(), [])
            self.assertEqual(memory.get_people(), {"people": {}})
            self.assertTrue(Path(memory.memory_dir).is_dir())

    def test_hot_memory_can_store_assistant_only_internal_event(self):
        with tempfile.TemporaryDirectory() as folder:
            store = HotConversationStore(Path(folder) / "hot.jsonl")
            store.append_turn("hello", "hey")
            store.append_assistant_event("proactive hello", source="proactive:idle")
            self.assertEqual(
                store.load_recent(),
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hey"},
                    {"role": "assistant", "content": "proactive hello"},
                ],
            )

    def test_internal_event_uses_shared_luna_without_tools_or_fake_user_history(self):
        with tempfile.TemporaryDirectory() as folder:
            hot = HotConversationStore(Path(folder) / "hot.jsonl")
            luna = FakeLuna([LunaReply(text="Bir süredir ses yok, nasılsın?")])
            registry = ToolRegistry()
            devices = DeviceRegistry()
            local = LocalDeviceAgent(devices, lambda capability, args: {"ok": True}, set())
            core = AgentCore(
                luna=luna,
                system_prompt="EPIS identity",
                context_builder=FakeContext(),
                memory=FakeMemory(),
                registry=registry,
                devices=devices,
                local_agent=local,
                permissions=PermissionEngine(),
                hot_memory=hot,
            )
            try:
                turn = core.handle_internal_event("idle", "120 dakikadir mesaj yok")
                self.assertEqual(turn.message, "Bir süredir ses yok, nasılsın?")
                self.assertEqual(luna.calls[0][1], [])
                self.assertEqual(
                    hot.load_recent(),
                    [{"role": "assistant", "content": "Bir süredir ses yok, nasılsın?"}],
                )
            finally:
                core.close()

    def test_trusted_context_returns_only_pseudonymized_payload(self):
        class Memory:
            pass

        class Builder:
            def __init__(self, memory):
                pass

            def build(self, query):
                return "Emir bugün Ali ile Ankara'da konuştu. emir@example.com"

        class Privacy:
            def anonymize(self, text):
                return "[KNOWN_USER] bugün [KISI_1] ile [YER_1]'de konuştu. [EMAIL]", {
                    "[KNOWN_USER]": "Emir",
                    "[KISI_1]": "Ali",
                    "[YER_1]": "Ankara",
                }

        with patch(
            "agentic.trusted_context._load_legacy_context_modules",
            return_value=(Memory, Builder, Privacy),
        ):
            result = _personal_context({"query": "bugün ne oldu"})

        self.assertTrue(result["ok"])
        self.assertIn("[KNOWN_USER]", result["safe_context"])
        self.assertNotIn("Emir", result["safe_context"])
        self.assertFalse(result["reverse_mapping_exported"])
        self.assertNotIn("mapping", result)

    def test_registry_exposes_personal_context_capability(self):
        registry = build_local_registry()
        entry = registry.get("get_personal_context")
        self.assertIsNotNone(entry)
        spec, _ = entry
        self.assertEqual(spec.capability, "personal.context.read")
        self.assertIn("read", spec.effects)

    def test_proactive_http_endpoint_uses_shared_core(self):
        fake = SimpleNamespace(
            handle_internal_event=Mock(
                return_value=SimpleNamespace(message="Gün bitiyor, nasıl geçti?", tool_results=[])
            )
        )
        old_core = server_app._core
        old_token = server_app.INTERNAL_EVENT_TOKEN
        server_app._core = fake
        server_app.INTERNAL_EVENT_TOKEN = "internal-secret"
        self.addCleanup(setattr, server_app, "_core", old_core)
        self.addCleanup(setattr, server_app, "INTERNAL_EVENT_TOKEN", old_token)

        with TestClient(server_app.app) as client:
            response = client.post(
                "/internal/proactive",
                headers={"Authorization": "Bearer internal-secret"},
                json={
                    "trigger_type": "end_of_day",
                    "context": "Gün sonuna gelindi.",
                    "priority": "medium",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Gün bitiyor, nasıl geçti?")
        fake.handle_internal_event.assert_called_once()

    def test_whatsapp_http_endpoint_uses_shared_core(self):
        fake = SimpleNamespace(
            handle=Mock(
                return_value=SimpleNamespace(
                    message="Selam, buradayım.",
                    confirmation_required=False,
                    approval=None,
                    tool_results=[],
                )
            )
        )
        old_core = server_app._core
        old_secret = server_app.WEBHOOK_SHARED_SECRET
        server_app._core = fake
        server_app.WEBHOOK_SHARED_SECRET = "wa-secret"
        self.addCleanup(setattr, server_app, "_core", old_core)
        self.addCleanup(setattr, server_app, "WEBHOOK_SHARED_SECRET", old_secret)

        with TestClient(server_app.app) as client:
            response = client.post(
                "/whatsapp/incoming",
                headers={"X-EPIS-Webhook-Secret": "wa-secret"},
                json={"from": "+905551112233", "body": "selam", "message_id": "wa-msg-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reply"], "Selam, buradayım.")
        args = fake.handle.call_args.args
        self.assertEqual(args[0], "selam")
        self.assertEqual(args[1], "wa-msg-1")
        self.assertTrue(args[2].startswith("whatsapp:"))

    def test_legacy_chat_entrypoints_are_unified_server_aliases(self):
        for relative in (
            "Layer-2/src/epis_chat.py",
            "Layer-2/src/whatsapp_webhook.py",
        ):
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertIn("from server.app import app", text)
            self.assertNotIn("Layer1Engine(", text)
            self.assertNotIn("EpisRouter(", text)


    def test_proactive_delivery_uses_shared_runtime_not_local_engine(self):
        from proactive_delivery import ProactiveDelivery

        delivery = ProactiveDelivery(memory=None, habits={
            "proactive": {"instant_push": True, "channel": "ui", "quiet_hours_enabled": False}
        })
        delivery.runtime = Mock()
        delivery.runtime.is_ready.return_value = True
        delivery.runtime.proactive.return_value = {
            "ok": True,
            "message": "Kisa bir check-in: nasilsin?",
            "request_id": "kairos:test",
            "delivered_clients": 1,
        }
        delivery.wa = Mock()
        delivery.band = Mock()
        delivery.band.is_ready.return_value = False

        result = delivery.push("idle", "120 dakikadir mesaj yok", "low")
        self.assertTrue(result["pushed"])
        self.assertIn("shared-session", result["via"])
        delivery.runtime.proactive.assert_called_once_with(
            "idle", "120 dakikadir mesaj yok", "low"
        )

    def test_cloud_factory_uses_cloud_runtime_memory(self):
        from agentic.cli import create_core

        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ,
            {
                "EPIS_DEPLOYMENT": "cloud",
                "EPIS_LUNA_CONTEXT_MODE": "minimal",
                "EPIS_CLOUD_RUNTIME_DIR": folder,
            },
            clear=False,
        ):
            core = create_core()
            try:
                self.assertIsInstance(core.memory, CloudRuntimeMemory)
                self.assertEqual(core.context_builder.build_minimal()[:8], "## ZAMAN")
            finally:
                core.close()

    def test_cloud_factory_rejects_automatic_private_context_mode(self):
        from agentic.cli import create_core

        with patch.dict(
            os.environ,
            {
                "EPIS_DEPLOYMENT": "cloud",
                "EPIS_LUNA_CONTEXT_MODE": "local",
            },
            clear=False,
        ):
            with self.assertRaisesRegex(ValueError, "trusted device capability"):
                create_core()

    def test_legacy_layer1_is_fail_closed_in_cloud(self):
        from epis_core import Layer1Engine

        with patch.dict(os.environ, {"EPIS_DEPLOYMENT": "cloud"}):
            with self.assertRaisesRegex(RuntimeError, "disabled in cloud mode"):
                Layer1Engine("test")


if __name__ == "__main__":
    unittest.main()
