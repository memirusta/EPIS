import sys
from pathlib import Path
import unittest
from unittest.mock import Mock

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "Layer-2"
        / "src"
    ),
)

from agentic.capability_broker import (
    CapabilityBroker,
    CapabilitySpec,
    default_capability_specs,
)
from agentic.luna import ToolCall
from test_agentic_core import build_core


SCHEMA = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "maxLength": 100,
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


class FakeProvider:
    def __init__(
        self,
        provider_id,
        responses,
        *,
        priority=100,
        available=True,
    ):
        self.provider_id = provider_id
        self.display_name = provider_id
        self.priority = priority
        self.responses = list(responses)
        self.is_available = available
        self.calls = []

    def available(self):
        return self.is_available

    def capabilities(self):
        return {"web.research"}

    def execute(self, capability, arguments):
        self.calls.append((capability, arguments))
        return self.responses.pop(0)

    def close(self):
        return None


class FakeComputerProvider:
    provider_id = "computer-test"
    display_name = "Computer Test"
    priority = 1

    def __init__(self):
        self.calls = []

    def available(self):
        return True

    def capabilities(self):
        return {"computer.execute"}

    def execute(self, capability, arguments):
        self.calls.append((capability, dict(arguments)))
        return {
            "ok": True,
            "status": "computer_goal_completed",
            "visual_completion_reported": True,
        }

    def close(self):
        return None


def computer_broker(provider):
    broker = CapabilityBroker()
    spec = next(
        item
        for item in default_capability_specs()
        if item.name == "computer_execute_goal"
    )
    broker.register_spec(spec)
    broker.register_provider(provider)
    return broker


def broker_with(*providers):
    broker = CapabilityBroker()
    broker.register_spec(
        CapabilitySpec(
            "web_research",
            "Research current web.",
            SCHEMA,
            "web.research",
            "web",
        )
    )
    for provider in providers:
        broker.register_provider(provider)
    return broker


class CapabilityBrokerTests(unittest.TestCase):
    def test_computer_goal_contract_is_semantic_and_confirmed_once(self):
        spec = next(
            item
            for item in default_capability_specs()
            if item.name == "computer_execute_goal"
        )
        self.assertEqual(spec.capability, "computer.execute")
        self.assertTrue(spec.confirmation_required)
        self.assertEqual(spec.risk_class, "yellow")
        self.assertNotIn("coordinate", spec.schema["properties"])

    def test_explicit_current_turn_skips_duplicate_computer_approval(self):
        provider = FakeComputerProvider()
        core = build_core([])
        self.addCleanup(core.close)
        core.capability_broker = computer_broker(provider)

        turn = core._dispatch_broker(
            ToolCall(
                "send-now",
                "computer_execute_goal",
                {
                    "goal": "Send the currently drafted message and verify it.",
                    "target_app": "ChatGPT",
                },
            ),
            False,
            user_message="Mesajı da gönderir misin?",
        )
        self.assertFalse(turn.confirmation_required)
        self.assertEqual(len(provider.calls), 1)

    def test_assistant_proposed_send_still_requires_approval(self):
        provider = FakeComputerProvider()
        core = build_core([])
        self.addCleanup(core.close)
        core.capability_broker = computer_broker(provider)

        turn = core._dispatch_broker(
            ToolCall(
                "send-proposed",
                "computer_execute_goal",
                {
                    "goal": "Send a message to the project owner.",
                    "target_app": "ChatGPT",
                },
            ),
            False,
            user_message="Bu bugı incele.",
        )
        self.assertTrue(turn.confirmation_required)
        self.assertEqual(
            turn.approval["authorization_category"],
            "external_communication",
        )
        self.assertEqual(provider.calls, [])

    def test_only_live_provider_tools_are_exposed(self):
        unavailable = FakeProvider(
            "offline",
            [],
            available=False,
        )
        broker = broker_with(unavailable)
        self.assertEqual(broker.openai_schemas(), [])

        live = FakeProvider(
            "live",
            [{"ok": True}],
            priority=10,
        )
        broker.register_provider(live)
        names = {
            item["function"]["name"]
            for item in broker.openai_schemas()
        }
        self.assertEqual(names, {"web_research"})

    def test_provider_priority_and_explicit_unavailable_fallback(self):
        first = FakeProvider(
            "first",
            [{
                "ok": False,
                "error": "provider_unavailable",
            }],
            priority=1,
        )
        second = FakeProvider(
            "second",
            [{
                "ok": True,
                "answer": "done",
            }],
            priority=2,
        )
        result = broker_with(
            first,
            second,
        ).dispatch(
            "web_research",
            {"query": "x"},
        )
        self.assertEqual(result.provider_id, "second")
        self.assertTrue(result.result["ok"])
        self.assertEqual(len(first.calls), 1)
        self.assertEqual(len(second.calls), 1)

    def test_broker_never_falls_back_after_unknown_outcome(self):
        first = FakeProvider(
            "first",
            [{
                "ok": False,
                "outcome": "unknown",
                "error": "network_lost",
            }],
            priority=1,
        )
        second = FakeProvider(
            "second",
            [{"ok": True}],
            priority=2,
        )
        result = broker_with(
            first,
            second,
        ).dispatch(
            "web_research",
            {"query": "x"},
        )
        self.assertEqual(result.provider_id, "first")
        self.assertEqual(len(second.calls), 0)

    def test_broker_does_not_hide_ordinary_provider_failure(self):
        first = FakeProvider(
            "first",
            [{
                "ok": False,
                "error": "bad_request",
            }],
            priority=1,
        )
        second = FakeProvider(
            "second",
            [{"ok": True}],
            priority=2,
        )
        result = broker_with(
            first,
            second,
        ).dispatch(
            "web_research",
            {"query": "x"},
        )
        self.assertEqual(result.provider_id, "first")
        self.assertEqual(len(second.calls), 0)

    def test_core_routes_semantic_capability_without_device_dispatch(self):
        provider = FakeProvider(
            "hosted",
            [{
                "ok": True,
                "answer": "fresh result",
            }],
        )
        broker = broker_with(provider)
        core = build_core([])
        self.addCleanup(core.close)
        core.capability_broker = broker
        core.local_agent.execute = Mock()

        tools = {
            item["function"]["name"]
            for item in core._model_tools()
            if item.get("type") == "function"
        }
        self.assertIn("web_research", tools)

        turn = core._dispatch(
            ToolCall(
                "web-1",
                "web_research",
                {"query": "announcement"},
            ),
            False,
        )
        self.assertEqual(
            turn.tool_results[0]["answer"],
            "fresh result",
        )
        core.local_agent.execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
