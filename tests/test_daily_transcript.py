from pathlib import Path
import sys
import tempfile
import unittest

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "Layer-2" / "src"))

from server.daily_transcript import (
    SqliteDailyTranscriptStore,
    day_id_for,
    transcript_input_hash,
)
import server.app as server_app


class DailyTranscriptStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SqliteDailyTranscriptStore(
            Path(self.temp.name) / "daily.sqlite3",
            durable=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_append_lists_canonical_order_and_metadata(self):
        first = self.store.append_message(
            role="user",
            text="PC mesajı",
            request_id="req-1",
            client_id="desktop-1",
            origin_device_id="desktop:desktop-1",
            attachments=[
                {"name": "note.txt", "mime_type": "text/plain", "size_bytes": 5}
            ],
        )
        second = self.store.append_message(
            role="assistant",
            text="PC cevabı",
            request_id="req-1",
            client_id="desktop-1",
        )

        messages = self.store.list_messages()
        self.assertEqual([m["text"] for m in messages], ["PC mesajı", "PC cevabı"])
        self.assertLess(first["seq"], second["seq"])
        self.assertEqual(messages[0]["attachments"][0]["name"], "note.txt")
        self.assertEqual(messages[0]["request_id"], "req-1")
        self.assertEqual(messages[0]["day_id"], day_id_for())

    def test_import_only_when_day_is_empty(self):
        imported = self.store.import_if_empty(
            [
                {"role": "user", "text": "eski desktop mesajı"},
                {"role": "assistant", "text": "eski cevap"},
            ]
        )
        self.assertEqual(len(imported), 2)
        self.assertEqual(
            self.store.import_if_empty([{"role": "user", "text": "duplicate"}]),
            [],
        )
        self.assertEqual(
            [m["text"] for m in self.store.list_messages()],
            ["eski desktop mesajı", "eski cevap"],
        )

    def test_context_boundary_preserves_daily_raw_but_resets_model_context(self):
        self.store.append_message(role="user", text="ilk bağlam")
        self.store.append_message(role="assistant", text="ilk cevap")
        self.assertEqual(len(self.store.list_context_messages()), 2)
        self.store.mark_context_boundary()
        self.assertEqual(self.store.list_context_messages(), [])
        self.assertEqual(len(self.store.list_messages()), 2)
        self.store.append_message(role="user", text="yeni bağlam")
        self.assertEqual(
            [item["text"] for item in self.store.list_context_messages()],
            ["yeni bağlam"],
        )
        self.assertEqual(len(self.store.list_messages()), 3)

    def test_freeze_hash_is_idempotent_and_blocks_new_messages(self):
        self.store.append_message(role="user", text="günlük veri")
        frozen = self.store.freeze_day()
        again = self.store.freeze_day()
        self.assertEqual(frozen["input_hash"], again["input_hash"])
        self.assertEqual(
            frozen["input_hash"],
            transcript_input_hash(frozen["messages"]),
        )
        with self.assertRaises(RuntimeError):
            self.store.append_message(role="assistant", text="geç mesaj")

    def test_pending_days_are_ordered_and_exclude_processed_days(self):
        self.store.append_message(role="user", text="older", day_id="2026-09-20")
        frozen = self.store.freeze_day("2026-09-20")
        self.store.append_message(role="user", text="newer", day_id="2026-09-21")

        pending = self.store.list_pending_days(before_day_id="2026-09-22")
        self.assertEqual([row["day_id"] for row in pending], ["2026-09-20", "2026-09-21"])
        self.assertEqual(pending[0]["status"], "frozen")
        self.assertEqual(pending[1]["status"], "active")

        self.assertTrue(self.store.mark_processed("2026-09-20", frozen["input_hash"]))
        after = self.store.list_pending_days(before_day_id="2026-09-22")
        self.assertEqual([row["day_id"] for row in after], ["2026-09-21"])

    def test_raw_messages_cannot_be_purged_before_verified_processing(self):
        self.store.append_message(role="user", text="silinmemeli")
        frozen = self.store.freeze_day()
        with self.assertRaises(RuntimeError):
            self.store.purge_processed(frozen["day_id"], frozen["input_hash"])
        self.assertTrue(
            self.store.mark_processed(frozen["day_id"], frozen["input_hash"])
        )
        self.assertEqual(
            self.store.purge_processed(frozen["day_id"], frozen["input_hash"]),
            1,
        )
        self.assertEqual(self.store.list_messages(frozen["day_id"]), [])
        again = self.store.freeze_day(frozen["day_id"])
        self.assertEqual(again["status"], "processed")
        self.assertTrue(again["already_processed"])
        self.assertEqual(again["input_hash"], frozen["input_hash"])
        self.assertTrue(
            self.store.mark_processed(frozen["day_id"], frozen["input_hash"])
        )
        self.assertEqual(
            self.store.purge_processed(frozen["day_id"], frozen["input_hash"]),
            0,
        )


class DailyTranscriptWebsocketTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.previous_store = server_app._transcript_store
        self.previous_internal_token = server_app.INTERNAL_EVENT_TOKEN
        server_app.INTERNAL_EVENT_TOKEN = "phase-b-test-token"
        server_app._transcript_store = SqliteDailyTranscriptStore(
            Path(self.temp.name) / "server.sqlite3",
            durable=True,
        )

    def tearDown(self):
        server_app._transcript_store = self.previous_store
        server_app.INTERNAL_EVENT_TOKEN = self.previous_internal_token
        self.temp.cleanup()

    def test_desktop_import_bootstraps_empty_day_and_mobile_sync_sees_it(self):
        with TestClient(server_app.app) as client:
            with client.websocket_connect("/ws") as desktop:
                self.assertEqual(desktop.receive_json()["type"], "connected")
                desktop.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "desktop-sync-test",
                    "client_type": "desktop",
                })
                self.assertEqual(desktop.receive_json()["type"], "client.ready")
                desktop.send_json({
                    "type": "conversation.import",
                    "request_id": "import-1",
                    "messages": [
                        {"role": "user", "text": "sabah PC"},
                        {"role": "assistant", "text": "sabah cevap"},
                    ],
                })
                self.assertEqual(desktop.receive_json()["type"], "conversation.imported")
                snapshot = desktop.receive_json()
                self.assertEqual(snapshot["type"], "conversation.snapshot")
                self.assertEqual(snapshot["store"]["backend"], "sqlite")
                self.assertTrue(snapshot["store"]["durable"])

            with client.websocket_connect("/ws") as mobile:
                self.assertEqual(mobile.receive_json()["type"], "connected")
                mobile.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "mobile-sync-test",
                    "client_type": "mobile",
                })
                self.assertEqual(mobile.receive_json()["type"], "client.ready")
                mobile.send_json({
                    "type": "conversation.sync",
                    "request_id": "sync-1",
                })
                synced = mobile.receive_json()
                self.assertEqual(synced["type"], "conversation.snapshot")
                self.assertEqual(
                    [item["text"] for item in synced["messages"]],
                    ["sabah PC", "sabah cevap"],
                )
                self.assertTrue(all(item.get("message_id") for item in synced["messages"]))
                self.assertTrue(all(item.get("seq") for item in synced["messages"]))

    def test_internal_nc_endpoints_freeze_ack_and_purge_exact_receipt(self):
        store = server_app.get_transcript_store()
        store.append_message(role="user", text="NC raw", day_id="2026-09-20")
        headers = {"Authorization": "Bearer phase-b-test-token"}
        with TestClient(server_app.app) as client:
            pending = client.get(
                "/internal/transcript/pending?before_day_id=2026-09-21",
                headers=headers,
            )
            self.assertEqual(pending.status_code, 200)
            self.assertEqual(pending.json()["days"][0]["day_id"], "2026-09-20")

            frozen_response = client.post(
                "/internal/transcript/freeze",
                headers=headers,
                json={"day_id": "2026-09-20"},
            )
            self.assertEqual(frozen_response.status_code, 200)
            frozen = frozen_response.json()["transcript"]
            self.assertEqual(frozen["status"], "frozen")
            self.assertEqual(len(frozen["messages"]), 1)

            wrong = client.post(
                "/internal/transcript/processed",
                headers=headers,
                json={"day_id": "2026-09-20", "input_hash": "0" * 64, "purge": True},
            )
            self.assertEqual(wrong.status_code, 409)
            self.assertEqual(len(store.list_messages("2026-09-20")), 1)

            ack = client.post(
                "/internal/transcript/processed",
                headers=headers,
                json={"day_id": "2026-09-20", "input_hash": frozen["input_hash"], "purge": True},
            )
            self.assertEqual(ack.status_code, 200)
            self.assertEqual(ack.json()["purged"], 1)
            self.assertEqual(store.list_messages("2026-09-20"), [])

    def test_mobile_cannot_import_local_history(self):
        with TestClient(server_app.app) as client:
            with client.websocket_connect("/ws") as mobile:
                mobile.receive_json()
                mobile.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "mobile-import-test",
                    "client_type": "mobile",
                })
                mobile.receive_json()
                mobile.send_json({
                    "type": "conversation.import",
                    "request_id": "import-mobile",
                    "messages": [{"role": "user", "text": "nope"}],
                })
                payload = mobile.receive_json()
                self.assertEqual(payload["type"], "error")
                self.assertEqual(payload["error"], "conversation_import_not_allowed")

    def test_clients_are_wired_for_full_snapshot_and_live_sync(self):
        desktop = (ROOT / "Layer-2" / "desktop-ui" / "src" / "App.tsx").read_text(
            encoding="utf-8"
        )
        mobile = (ROOT / "Layer-2" / "mobile" / "lib" / "src" / "controller.dart").read_text(
            encoding="utf-8"
        )
        self.assertIn('type: "conversation.sync"', desktop)
        self.assertIn('data.type === "conversation.snapshot"', desktop)
        self.assertIn('data.type === "conversation.live_message"', desktop)
        self.assertIn('type: "conversation.import"', desktop)
        self.assertIn("case 'conversation.snapshot':", mobile)
        self.assertIn("case 'conversation.live_message':", mobile)
        self.assertIn("preserved_daily_transcript", desktop)
        self.assertIn("case 'conversation.reset':", mobile)
        self.assertIn("preserved_daily_transcript", mobile)
        core = (ROOT / "Layer-2" / "src" / "agentic" / "core.py").read_text(encoding="utf-8")
        self.assertIn("hydrate_conversation_if_empty", core)


if __name__ == "__main__":
    unittest.main()
