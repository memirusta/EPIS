import sys
from pathlib import Path
import unittest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "Layer-2"
        / "src"
    ),
)

from agentic.computer_use import (
    OpenAIComputerUseProvider,
)


class FakeItem:
    def __init__(self, data):
        self.data = data

    def model_dump(self, exclude_none=True):
        return dict(self.data)


class FakeResponse:
    def __init__(
        self,
        output,
        *,
        text="",
        status="completed",
    ):
        self.output = [
            FakeItem(item)
            for item in output
        ]
        self.output_text = text
        self.status = status
        self.usage = None


class FakeResponses:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.responses = FakeResponses(
            responses
        )


class FakeBridge:
    def __init__(self):
        self.prepared = []
        self.actions = []
        self.capture_count = 0

    def available(self):
        return True

    def prepare(self, target_app):
        self.prepared.append(target_app)
        return {
            "device_id": "pc",
            "window_id": "w",
            "app_name": "app.exe",
        }, None

    def capture(self, session):
        self.capture_count += 1
        payload = (
            "aW1hZ2U="
        )
        return {
            "ok": True,
            "frame_id": (
                f"{self.capture_count:032x}"
            ),
            "mime_type": "image/jpeg",
            "image_base64": payload,
            "width": 800,
            "height": 600,
            "sha256": (
                f"digest-{self.capture_count}"
            ),
        }

    def apply_actions(
        self,
        session,
        frame,
        actions,
    ):
        self.actions.append(
            (frame["frame_id"], actions)
        )
        return {
            "ok": True,
            "actions_executed": len(actions),
        }


class ComputerUseProviderTests(unittest.TestCase):
    def test_stateless_screenshot_action_loop_completes_goal(self):
        bridge = FakeBridge()
        client = FakeClient([
            FakeResponse([{
                "type": "computer_call",
                "call_id": "call-1",
                "actions": [{
                    "type": "screenshot",
                }],
                "status": "completed",
            }]),
            FakeResponse([{
                "type": "computer_call",
                "call_id": "call-2",
                "actions": [
                    {
                        "type": "click",
                        "button": "left",
                        "x": 20,
                        "y": 30,
                    },
                    {
                        "type": "type",
                        "text": "hello",
                    },
                ],
                "status": "completed",
            }]),
            FakeResponse(
                [],
                text="VERIFIED: Goal complete.",
            ),
        ])
        provider = OpenAIComputerUseProvider(
            bridge,
            api_key="test",
            client_factory=lambda: client,
        )

        result = provider.execute(
            "computer.execute",
            {
                "goal": "Open a new chat and type hello.",
                "target_app": "ChatGPT",
            },
        )
        self.assertTrue(result["ok"])
        self.assertEqual(
            bridge.prepared,
            ["ChatGPT"],
        )
        self.assertEqual(
            len(bridge.actions),
            1,
        )
        self.assertEqual(
            len(client.responses.calls),
            3,
        )
        self.assertFalse(
            client.responses.calls[0]["store"]
        )
        second_input = (
            client.responses.calls[1]["input"]
        )
        self.assertTrue(
            any(
                item.get("type")
                == "computer_call_output"
                for item in second_input
                if isinstance(item, dict)
            )
        )

    def test_unverified_completion_fails_closed(self):
        bridge = FakeBridge()
        client = FakeClient([
            FakeResponse([{
                "type": "computer_call",
                "call_id": "screen",
                "actions": [{"type": "screenshot"}],
            }]),
            FakeResponse(
                [],
                text="UNVERIFIED: submission was not visible.",
            ),
        ])
        provider = OpenAIComputerUseProvider(
            bridge,
            api_key="test",
            client_factory=lambda: client,
        )

        result = provider.execute(
            "computer.execute",
            {"goal": "Send the drafted message and verify it."},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "computer_goal_unverified")
        self.assertFalse(result["visual_completion_reported"])

    def test_model_cannot_act_before_first_screenshot(self):
        bridge = FakeBridge()
        client = FakeClient([
            FakeResponse([{
                "type": "computer_call",
                "call_id": "blind",
                "actions": [{
                    "type": "click",
                    "button": "left",
                    "x": 1,
                    "y": 1,
                }],
            }]),
        ])
        provider = OpenAIComputerUseProvider(
            bridge,
            api_key="test",
            client_factory=lambda: client,
        )
        result = provider.execute(
            "computer.execute",
            {"goal": "click"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "computer_initial_screenshot_required",
        )
        self.assertEqual(
            bridge.actions,
            [],
        )

    def test_provider_safety_check_stops_before_device_action(self):
        bridge = FakeBridge()
        client = FakeClient([
            FakeResponse([{
                "type": "computer_call",
                "call_id": "safe",
                "actions": [{
                    "type": "screenshot",
                }],
                "pending_safety_checks": [{
                    "id": "check",
                }],
            }]),
        ])
        provider = OpenAIComputerUseProvider(
            bridge,
            api_key="test",
            client_factory=lambda: client,
        )
        result = provider.execute(
            "computer.execute",
            {"goal": "do thing"},
        )
        self.assertFalse(result["ok"])
        self.assertEqual(
            result["error"],
            "computer_provider_safety_check_required",
        )
        self.assertEqual(
            bridge.actions,
            [],
        )

    def test_provider_is_live_only_with_key_and_device_bridge(self):
        bridge = FakeBridge()
        provider = OpenAIComputerUseProvider(
            bridge,
            api_key="test",
            client_factory=lambda: FakeClient([]),
        )
        self.assertTrue(provider.available())
        self.assertEqual(
            provider.capabilities(),
            {"computer.execute"},
        )

        bridge.available = lambda: False
        self.assertFalse(provider.available())


if __name__ == "__main__":
    unittest.main()
