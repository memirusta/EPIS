import sys
from pathlib import Path
import threading
import unittest

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "Layer-2" / "src"),
)

from agentic.core import AgentCore
from agentic.devices import DeviceRegistry, LocalDeviceAgent
from agentic.luna import LunaReply, ToolCall
from agentic.permissions import PermissionEngine
from agentic.tools import ToolRegistry, ToolSpec


class FakeContext:
    def build_minimal(self):
        return ""

    def build(self, _message):
        return ""


class FakeMemory:
    def log_interaction(self, *_args, **_kwargs):
        return None


class QueueLuna:
    def __init__(self, replies):
        self.replies = list(replies)
        self.lock = threading.Lock()

    def complete(self, _messages, tools):
        if not tools:
            return LunaReply(text="Bu işlem için onay gerekiyor.")
        with self.lock:
            if not self.replies:
                raise AssertionError("No fake Luna reply left")
            return self.replies.pop(0)


class BarrierLuna:
    def __init__(self):
        self.barrier = threading.Barrier(2)

    def complete(self, messages, tools):
        if not tools:
            return LunaReply(text="Onay gerekiyor.")
        user = next(
            (
                str(message.get("content") or "")
                for message in reversed(messages)
                if message.get("role") == "user"
            ),
            "",
        )
        self.barrier.wait(timeout=3)
        return LunaReply(text=f"reply:{user}")


def build_core(luna, *, yellow=False):
    registry = ToolRegistry()
    capabilities = set()

    if yellow:
        capability = "test.confirm"
        capabilities.add(capability)
        registry.register(
            ToolSpec(
                "confirming_tool",
                "Test confirmation tool.",
                {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False,
                },
                capability,
                "yellow",
                True,
            ),
            lambda _args: {"ok": True, "status": "executed"},
        )

    devices = DeviceRegistry()
    local = LocalDeviceAgent(
        devices,
        lambda capability, _args: {
            "ok": capability in capabilities,
            "status": "executed",
        },
        capabilities,
    )

    return AgentCore(
        luna=luna,
        system_prompt="EPIS",
        context_builder=FakeContext(),
        memory=FakeMemory(),
        registry=registry,
        devices=devices,
        local_agent=local,
        permissions=PermissionEngine(),
    )


class UnifiedRuntimePhase2Tests(unittest.TestCase):
    def test_two_clients_keep_independent_pending_approvals(self):
        core = build_core(
            QueueLuna(
                [
                    LunaReply(
                        tool_calls=[
                            ToolCall("call-a", "confirming_tool", {})
                        ]
                    ),
                    LunaReply(
                        tool_calls=[
                            ToolCall("call-b", "confirming_tool", {})
                        ]
                    ),
                    LunaReply(text="A tamam"),
                    LunaReply(text="B tamam"),
                ]
            ),
            yellow=True,
        )
        self.addCleanup(core.close)

        first = core.handle(
            "A işlemini yap",
            "request-a",
            "desktop-a",
            "desktop:desktop-a",
        )
        second = core.handle(
            "B işlemini yap",
            "request-b",
            "mobile-b",
            "mobile:mobile-b",
        )

        self.assertTrue(first.confirmation_required)
        self.assertTrue(second.confirmation_required)
        self.assertEqual(core.pending_count(), 2)

        first_id = first.approval["id"]
        second_id = second.approval["id"]

        self.assertEqual(
            core.pending_metadata(first_id)["client_id"],
            "desktop-a",
        )
        self.assertEqual(
            core.pending_metadata(second_id)["client_id"],
            "mobile-b",
        )

        wrong_client = core.confirm_pending(
            first_id,
            "mobile-b",
        )
        self.assertIn(
            "geçerli değil",
            wrong_client.message,
        )
        self.assertEqual(core.pending_count(), 2)

        confirmed_a = core.confirm_pending(
            first_id,
            "desktop-a",
        )
        self.assertEqual(confirmed_a.message, "A tamam")
        self.assertEqual(core.pending_count(), 1)
        self.assertIsNotNone(
            core.pending_metadata(second_id)
        )

        confirmed_b = core.confirm_pending(
            second_id,
            "mobile-b",
        )
        self.assertEqual(confirmed_b.message, "B tamam")
        self.assertEqual(core.pending_count(), 0)

    def test_concurrent_turns_share_history_without_overwrite(self):
        core = build_core(BarrierLuna())
        self.addCleanup(core.close)
        output = {}
        errors = []

        def run(name):
            try:
                output[name] = core.handle(
                    name,
                    f"request-{name}",
                    f"client-{name}",
                    f"device-{name}",
                )
            except Exception as exc:  # pragma: no cover - test diagnostic
                errors.append(exc)

        first = threading.Thread(target=run, args=("one",))
        second = threading.Thread(target=run, args=("two",))
        first.start()
        second.start()
        first.join(timeout=5)
        second.join(timeout=5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(
            {output["one"].message, output["two"].message},
            {"reply:one", "reply:two"},
        )
        self.assertEqual(
            sum(
                1
                for message in core.history
                if message.get("role") == "user"
            ),
            2,
        )
        self.assertEqual(
            sum(
                1
                for message in core.history
                if message.get("role") == "assistant"
            ),
            2,
        )
        self.assertEqual(core.active_turns(), [])

    def test_reset_refuses_running_turn(self):
        core = build_core(QueueLuna([LunaReply(text="done")]))
        self.addCleanup(core.close)

        core._register_active_turn(
            "request-live",
            "desktop",
            "desktop:desktop",
            "still running",
        )
        with self.assertRaisesRegex(
            RuntimeError,
            "conversation_busy",
        ):
            core.new_conversation()


if __name__ == "__main__":
    unittest.main()
