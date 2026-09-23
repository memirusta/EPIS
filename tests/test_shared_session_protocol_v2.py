import sys
from pathlib import Path
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"),
)

from fastapi.testclient import TestClient
import server.app as server_app


class FakeCore:
    def __init__(self):
        self.reset_calls = 0

    def handle(self, text, request_id, client_id, origin_device_id):
        time.sleep(0.08)
        return SimpleNamespace(
            message=f"reply:{text}",
            confirmation_required=False,
            approval=None,
            tool_results=[],
        )

    def public_devices(self):
        return []

    def pending_metadata(self, _approval_id):
        return None

    def confirm_pending(self, *_args):
        raise AssertionError("not used")

    def reject_pending(self, *_args):
        raise AssertionError("not used")

    def new_conversation(self):
        self.reset_calls += 1
        return 0




class ApprovalFakeCore(FakeCore):
    def __init__(self):
        super().__init__()
        self.confirmed = []
        self.pending = {
            "approval-a": {
                "approval_id": "approval-a",
                "request_id": "original-a",
                "client_id": "desktop-owner-a",
                "origin_device_id": "desktop:desktop-owner-a",
            }
        }

    def pending_metadata(self, approval_id):
        value = self.pending.get(approval_id)
        return dict(value) if value is not None else None

    def confirm_pending(self, approval_id, client_id):
        metadata = self.pending.get(approval_id)
        if metadata is None or metadata["client_id"] != client_id:
            return SimpleNamespace(
                message="invalid",
                confirmation_required=False,
                approval=None,
                tool_results=[],
            )
        self.confirmed.append((approval_id, client_id))
        self.pending.pop(approval_id, None)
        return SimpleNamespace(
            message="approved",
            confirmation_required=False,
            approval=None,
            tool_results=[{"ok": True}],
        )


class ProtocolV2Tests(unittest.TestCase):
    def test_client_hello_and_chat_ack_allow_multiple_inflight_requests(self):
        fake = FakeCore()

        with patch.object(server_app, "_core", fake):
            client = TestClient(server_app.app)
            with client.websocket_connect("/ws") as websocket:
                connected = websocket.receive_json()
                self.assertEqual(connected["type"], "connected")
                self.assertEqual(connected["protocol_version"], 2)

                websocket.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "desktop-test-v2",
                    "client_type": "desktop",
                })
                ready = websocket.receive_json()
                self.assertEqual(ready["type"], "client.ready")

                websocket.send_json({
                    "type": "chat.send",
                    "request_id": "req-v2-a",
                    "origin_device_id": "desktop:desktop-test-v2",
                    "text": "one",
                })
                websocket.send_json({
                    "type": "chat.send",
                    "request_id": "req-v2-b",
                    "origin_device_id": "desktop:desktop-test-v2",
                    "text": "two",
                })

                payloads = [
                    websocket.receive_json()
                    for _ in range(4)
                ]

                accepted = {
                    item["request_id"]
                    for item in payloads
                    if item.get("type") == "chat.accepted"
                }
                replies = {
                    item["request_id"]: item["text"]
                    for item in payloads
                    if item.get("type") == "assistant.message"
                }

                self.assertEqual(
                    accepted,
                    {"req-v2-a", "req-v2-b"},
                )
                self.assertEqual(
                    replies,
                    {
                        "req-v2-a": "reply:one",
                        "req-v2-b": "reply:two",
                    },
                )

    def test_reset_is_refused_while_accepted_chat_is_processing(self):
        fake = FakeCore()

        with patch.object(server_app, "_core", fake):
            client = TestClient(server_app.app)
            with client.websocket_connect("/ws") as websocket:
                websocket.receive_json()
                websocket.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "desktop-reset-race",
                    "client_type": "desktop",
                })
                websocket.receive_json()

                websocket.send_json({
                    "type": "chat.send",
                    "request_id": "req-live-before-reset",
                    "origin_device_id": "desktop:desktop-reset-race",
                    "text": "still working",
                })
                accepted = websocket.receive_json()
                self.assertEqual(accepted["type"], "chat.accepted")

                websocket.send_json({
                    "type": "conversation.new",
                    "request_id": "reset-while-live",
                })
                reset_accepted = websocket.receive_json()
                self.assertEqual(
                    reset_accepted["type"],
                    "conversation.accepted",
                )

                payloads = [
                    websocket.receive_json()
                    for _ in range(2)
                ]
                errors = [
                    item
                    for item in payloads
                    if item.get("type") == "error"
                ]
                replies = [
                    item
                    for item in payloads
                    if item.get("type") == "assistant.message"
                ]

                self.assertEqual(len(errors), 1)
                self.assertEqual(errors[0]["request_id"], "reset-while-live")
                self.assertEqual(errors[0]["detail"], "conversation_busy")
                self.assertEqual(len(replies), 1)
                self.assertEqual(
                    replies[0]["request_id"],
                    "req-live-before-reset",
                )
                self.assertEqual(fake.reset_calls, 0)


    def test_approval_is_owned_by_origin_client_and_ack_is_correlated(self):
        fake = ApprovalFakeCore()

        with patch.object(server_app, "_core", fake):
            client = TestClient(server_app.app)
            with client.websocket_connect("/ws") as owner, client.websocket_connect("/ws") as other:
                owner.receive_json()
                owner.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "desktop-owner-a",
                    "client_type": "desktop",
                })
                owner.receive_json()

                other.receive_json()
                other.send_json({
                    "type": "client.hello",
                    "version": 2,
                    "client_id": "mobile-other-a",
                    "client_type": "mobile",
                })
                other.receive_json()

                other.send_json({
                    "type": "approval.confirm",
                    "approval_id": "approval-a",
                    "operation_id": "wrong-operation-a",
                })
                wrong = other.receive_json()
                self.assertEqual(wrong["type"], "error")
                self.assertEqual(
                    wrong["error"],
                    "approval_not_owned_or_expired",
                )
                self.assertEqual(fake.confirmed, [])

                owner.send_json({
                    "type": "approval.confirm",
                    "approval_id": "approval-a",
                    "operation_id": "owner-operation-a",
                })
                accepted = owner.receive_json()
                self.assertEqual(accepted["type"], "approval.accepted")
                self.assertEqual(accepted["approval_id"], "approval-a")
                self.assertEqual(accepted["request_id"], "original-a")

                result = owner.receive_json()
                self.assertEqual(result["type"], "assistant.message")
                self.assertEqual(result["request_id"], "original-a")
                self.assertEqual(result["text"], "approved")
                self.assertEqual(
                    fake.confirmed,
                    [("approval-a", "desktop-owner-a")],
                )


if __name__ == "__main__":
    unittest.main()
