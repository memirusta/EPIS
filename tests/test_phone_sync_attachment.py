import asyncio
import base64
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from agentic.tools import build_local_registry
import server.app as server_app


class PhoneSyncAttachmentTests(unittest.TestCase):
    def test_phone_call_is_android_only_and_confirmation_bound(self):
        registry = build_local_registry()
        spec, _ = registry.get("phone_call")
        self.assertEqual(spec.capability, "phone.call")
        self.assertEqual(spec.platforms, ("android",))
        self.assertEqual(spec.risk_class, "yellow")
        self.assertTrue(spec.confirmation_required)
        self.assertEqual(registry.capabilities("android"), {"phone.call"})
        self.assertNotIn("phone.call", registry.capabilities("windows"))

    def test_android_device_hello_cannot_claim_windows_capability(self):
        allowed = {
            "windows": {"system.info"},
            "android": {"phone.call"},
        }
        device = server_app.device_from_hello(
            {
                "type": "device.hello",
                "version": 1,
                "device": {
                    "device_id": "android-test",
                    "display_name": "Galaxy",
                    "platform": "android",
                    "capabilities": ["phone.call"],
                },
            },
            allowed,
        )
        self.assertEqual(device.capabilities, {"phone.call"})
        with self.assertRaises(ValueError):
            server_app.device_from_hello(
                {
                    "type": "device.hello",
                    "version": 1,
                    "device": {
                        "device_id": "android-test",
                        "display_name": "Galaxy",
                        "platform": "android",
                        "capabilities": ["system.info"],
                    },
                },
                allowed,
            )

    def test_text_attachment_is_bounded_and_added_as_untrusted_context(self):
        attachment = {
            "name": "note.txt",
            "mime_type": "text/plain",
            "size_bytes": 5,
            "data_base64": base64.b64encode(b"hello").decode("ascii"),
        }
        model_text, display_text, summaries = asyncio.run(
            server_app.prepare_chat_input("Bunu oku", [attachment])
        )
        self.assertIn("Bunu oku", display_text)
        self.assertIn("note.txt", display_text)
        self.assertIn("<EPIS_ATTACHMENT_CONTEXT>", model_text)
        self.assertIn("untrusted user-supplied file content", model_text)
        self.assertIn("hello", model_text)
        self.assertEqual(summaries[0]["size_bytes"], 5)
        self.assertEqual(
            server_app._display_history_text(model_text),
            display_text,
        )

    def test_conversation_snapshot_strips_internal_attachment_context(self):
        previous = server_app._core
        server_app._core = SimpleNamespace(
            hot_memory=SimpleNamespace(
                load_recent=lambda: [
                    {
                        "role": "user",
                        "content": (
                            "Dosyaya bak\n📎 test.txt"
                            + server_app._ATTACHMENT_MARKER
                            + "\nsecret internal context"
                            + server_app._ATTACHMENT_END
                        ),
                    },
                    {"role": "assistant", "content": "Baktım."},
                ]
            ),
            history=[],
        )
        try:
            snapshot = server_app.conversation_payload()
        finally:
            server_app._core = previous
        self.assertEqual(snapshot[0]["text"], "Dosyaya bak\n📎 test.txt")
        self.assertEqual(snapshot[1]["text"], "Baktım.")

    def test_conversation_sync_websocket_returns_active_hot_session(self):
        previous = server_app._core
        server_app._core = SimpleNamespace(
            hot_memory=SimpleNamespace(
                load_recent=lambda: [
                    {"role": "user", "content": "PC mesajı"},
                    {"role": "assistant", "content": "PC cevabı"},
                ]
            ),
            history=[],
        )
        try:
            with TestClient(server_app.app) as client:
                with client.websocket_connect("/ws") as websocket:
                    self.assertEqual(websocket.receive_json()["type"], "connected")
                    websocket.send_json({
                        "type": "client.hello",
                        "version": 2,
                        "client_id": "mobile-test",
                        "client_type": "mobile",
                    })
                    self.assertEqual(websocket.receive_json()["type"], "client.ready")
                    websocket.send_json({
                        "type": "conversation.sync",
                        "request_id": "sync-test",
                    })
                    payload = websocket.receive_json()
            self.assertEqual(payload["type"], "conversation.snapshot")
            self.assertEqual(payload["messages"][0]["text"], "PC mesajı")
            self.assertEqual(payload["messages"][1]["text"], "PC cevabı")
        finally:
            server_app._core = previous

    def test_mobile_source_registers_device_agent_sync_and_attachment_button(self):
        mobile = ROOT / "Layer-2" / "mobile"
        agent = (mobile / "lib/src/android_device_agent.dart").read_text(encoding="utf-8")
        client = (mobile / "lib/src/epis_client.dart").read_text(encoding="utf-8")
        chat = (mobile / "lib/src/screens/chat_screen.dart").read_text(encoding="utf-8")
        native = (
            mobile
            / "android/app/src/main/kotlin/com/epis/epis_mobile/MainActivity.kt"
        ).read_text(encoding="utf-8")
        manifest = (mobile / "android/app/src/main/AndroidManifest.xml").read_text(encoding="utf-8")

        self.assertIn("'/device/ws'", agent)
        self.assertIn("'phone.call'", agent)
        self.assertIn("device.result", agent)
        self.assertIn("conversation.sync", client)
        self.assertIn("Dosya ekle", chat)
        self.assertIn("Icons.attach_file", chat)
        self.assertNotIn("Icons.add_comment_outlined", chat)
        self.assertIn("ACTION_CALL", native)
        self.assertIn("READ_CONTACTS", native)
        self.assertIn("CALL_PHONE", manifest)
        self.assertIn("READ_CONTACTS", manifest)


if __name__ == "__main__":
    unittest.main()
